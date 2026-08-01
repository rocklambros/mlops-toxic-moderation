"""DistilBERT multi-label fine-tune. This is the entrypoint that runs ON the GPU pod.

Three things in here are load-bearing, and each one is enforced by something that raises
rather than by a comment:

- **``problem_type="multi_label_classification"``.** Without it, ``AutoModelForSequence-
  Classification`` sets ``problem_type`` from the label dtype at the first forward pass and
  a six-column target lands on ``CrossEntropyLoss`` — softmax over six mutually exclusive
  classes. Nothing crashes. The loss falls, the run looks healthy, and the model has been
  trained on the wrong objective: it learns "which single label is most likely" on a corpus
  where 15,294 comments carry ``toxic`` AND ``obscene`` together. ``assert_bce_objective``
  runs a real forward pass and compares the model's own loss against
  ``binary_cross_entropy_with_logits`` computed by hand, so a softmax head fails loudly on
  the first batch instead of silently at the end of a paid GPU hour.

- **The cached Phase 0 bundle, never a recomputation.** ``prepare_dataset`` costs 13.6 min
  on the real corpus (docs/data-provenance.md). Recomputing it on the pod burns GPU-priced
  minutes on a CPU job, and a pod that resolves a different ``iterative-stratification``
  build silently trains against a different split from the classical model it will be
  compared with. The training path therefore CANNOT call ``prepare_dataset`` at all: the
  only importer of it is ``--build-cache``, which runs on the Jetson.

- **The held-out test set is not reachable from here.** DistilBERT is evaluated on
  validation folds. ``load_bundle_cache`` defaults to ``with_test=False`` and
  ``CachedBundle.test_df`` then raises ``HeldOutTestAccess``, because "picking the better of
  two test numbers" is selection on the test set and biases the winner upward (delivery spec
  section 6.1).

Torch, transformers and wandb are imported inside functions, never at module scope: this
module is unit-tested on an aarch64 build box that has none of them installed, and importing
``wandb`` reads ``~/.netrc`` and ``WANDB_API_KEY`` on a box that holds live keys.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from model.labels import LABELS
from model.seeds import run_metadata, set_all_seeds

MODEL_NAME = "distilbert-base-uncased"
PROBLEM_TYPE = "multi_label_classification"

# 192 word-pieces covers the corpus: Jigsaw comments are short, and the 99th percentile sits
# well inside this. Raising it costs quadratic attention time for a handful of long rows.
MAX_LENGTH = 192

# 2-3 epochs, weight decay, early stopping on validation. Delivery spec section 6.2 and the
# Phase 1 non-negotiables. More than three epochs on 180k rows overfits a 66M-parameter model
# on this corpus, which is exactly what the per-epoch gap below is there to make visible.
MIN_EPOCHS = 1
MAX_EPOCHS = 3
DEFAULT_EPOCHS = 3
DEFAULT_LR = 3e-5
DEFAULT_WEIGHT_DECAY = 0.01
DEFAULT_WARMUP_RATIO = 0.06
DEFAULT_BATCH_SIZE = 32
DEFAULT_EVAL_BATCH_SIZE = 128
DEFAULT_PATIENCE = 1

# The early-stopping criterion is threshold-free on purpose. macro-F1 needs thresholds, and
# thresholds are tuned out-of-fold in model/thresholds.py AFTER training; selecting a
# checkpoint on a metric that depends on a threshold this run has not tuned yet is circular.
DEFAULT_BEST_METRIC = "eval_macro_pr_auc"

# Forward-only probe over a fixed slice of the TRAINING rows, in eval mode, so the per-epoch
# train/val loss gap compares like with like. Trainer's running training loss is measured
# with dropout on and averaged across an epoch of changing weights, so it understates the gap.
DEFAULT_TRAIN_PROBE_ROWS = 4096

MANIFEST_NAME = "manifest.json"
TRAIN_FILE = "train.csv.gz"
TEST_FILE = "test.csv.gz"
FOLDS_FILE = "folds.npz"
CACHE_FORMAT = "mtm-bundle-cache/1"
SUMMARY_NAME = "training_summary.json"

# Anything matching these is a pickle by another name. safetensors, never pickle.
_PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".joblib", ".msgpack")


class BundleCacheError(RuntimeError):
    """The bundle cache is absent, incomplete, corrupt, or not the split that was asked for."""


class HeldOutTestAccess(RuntimeError):
    """Something reached for the held-out test rows from the training path."""


class ObjectiveError(RuntimeError):
    """The model is not optimising BCE-with-logits over six independent labels."""


class UnsafeArtifact(RuntimeError):
    """A pickle-format weight file is present in a directory that must be safetensors-only."""


# --------------------------------------------------------------------------------------
# Secrets. Point of use only, never to disk, never in argv.
# --------------------------------------------------------------------------------------


def load_secret(pass_name: str, env_var: str) -> str:
    """Env var first, then ``pass show``, with a 5 s timeout and a redacted failure.

    Mirrors the canonical RunPod helper. When ``infra.runpod.reap`` exists this defers to it
    so the two cannot drift; on a pod, where ``pass`` is not installed, the env var is the
    only path and the subprocess is never reached.
    """
    try:
        from infra.runpod.reap import load_secret as canonical  # noqa: PLC0415
    except ImportError:
        pass
    else:
        return canonical(pass_name, env_var)

    value = os.environ.get(env_var, "")
    if value:
        return value
    try:
        result = subprocess.run(
            ["pass", "show", pass_name],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"{env_var} is unset and `pass` is not installed; export {env_var} instead"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"'pass show {pass_name}' timed out after 5s") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"'pass show {pass_name}' failed (exit {exc.returncode}); secret not loaded"
        ) from None
    return result.stdout.strip()


# --------------------------------------------------------------------------------------
# The Phase 0 bundle cache
# --------------------------------------------------------------------------------------


def sha256_bytes(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_frame(df: pd.DataFrame, path: Path) -> None:
    # mtime=0 keeps the gzip bytes a function of the data alone, so a rebuilt cache with the
    # same split has the same digest and a changed digest means changed content.
    df.to_csv(path, index=False, compression={"method": "gzip", "mtime": 0})
    _fsync_file(path)


def _read_frame(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        dtype={"id": str, "comment_text": str},
        keep_default_na=False,
        compression="gzip",
    )
    missing = [c for c in ("id", "comment_text", *LABELS) if c not in df.columns]
    if missing:
        raise BundleCacheError(f"{path.name} is missing columns {missing}")
    for label in LABELS:
        df[label] = df[label].astype(int)
    return df


@dataclass(frozen=True, eq=False)
class CachedBundle:
    """The Phase 0 ``DatasetBundle``, minus anything the training path must not see."""

    train_df: pd.DataFrame
    fold_indices: list[tuple[np.ndarray, np.ndarray]]
    raw_sha256: str
    split_version: str
    env_version: str
    config: dict[str, Any]
    path: Path
    held_out: pd.DataFrame | None = None

    @property
    def data_version(self) -> str:
        joined = f"{self.raw_sha256}:{self.split_version}:{self.env_version}"
        return hashlib.sha256(joined.encode()).hexdigest()

    @property
    def test_df(self) -> pd.DataFrame:
        if self.held_out is None:
            raise HeldOutTestAccess(
                "the held-out test set is not reachable from the training path: DistilBERT is "
                "evaluated on validation folds, and choosing between classical and DistilBERT "
                "on test numbers is selection on the test set (delivery spec section 6.1). "
                "Load with with_test=True only from model/evaluate.py."
            )
        return self.held_out

    def fold(self, k: int) -> tuple[list[str], np.ndarray, list[str], np.ndarray]:
        """``(train_texts, y_train, val_texts, y_val)`` for outer fold ``k``."""
        if not 0 <= k < len(self.fold_indices):
            raise ValueError(f"fold {k} is out of range; the cache holds {len(self.fold_indices)}")
        train_idx, val_idx = self.fold_indices[k]
        overlap = np.intersect1d(train_idx, val_idx)
        if overlap.size:
            raise BundleCacheError(
                f"fold {k}: {overlap.size} rows appear in both the fit and the validation set"
            )
        texts = self.train_df["comment_text"].tolist()
        y = self.train_df[list(LABELS)].to_numpy().astype(np.float32)
        return (
            [texts[i] for i in train_idx],
            y[train_idx],
            [texts[i] for i in val_idx],
            y[val_idx],
        )


def write_bundle_cache(bundle, dest: Path, *, include_test: bool = True) -> Path:
    """Serialize a Phase 0 ``DatasetBundle`` to a directory. Runs once, on the Jetson.

    No pickle anywhere: gzipped CSV for the frames, ``.npz`` for the fold indices, JSON for
    the provenance. The manifest is written LAST and the whole directory is moved into place
    with ``os.replace``, so a crash mid-write cannot leave a directory that looks loadable.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.tmp-", dir=dest.parent))
    try:
        _write_frame(bundle.train_df, staging / TRAIN_FILE)
        if include_test:
            _write_frame(bundle.test_df, staging / TEST_FILE)

        folds = {}
        for k, (train_idx, val_idx) in enumerate(bundle.fold_indices):
            folds[f"fold{k}_train"] = np.asarray(train_idx, dtype=np.int64)
            folds[f"fold{k}_val"] = np.asarray(val_idx, dtype=np.int64)
        np.savez(staging / FOLDS_FILE, **folds)
        _fsync_file(staging / FOLDS_FILE)

        config = bundle.config
        manifest = {
            "format": CACHE_FORMAT,
            "raw_sha256": bundle.raw_sha256,
            "split_version": bundle.split_version,
            "env_version": bundle.env_version,
            "data_version": bundle.data_version,
            "config": {
                "seed": config.seed,
                "test_size": config.test_size,
                "n_folds": config.n_folds,
            },
            "labels": list(LABELS),
            "n_train": int(len(bundle.train_df)),
            "n_test": int(len(bundle.test_df)),
            "n_folds": len(bundle.fold_indices),
            "has_test": bool(include_test),
            "files": {
                name: sha256_bytes(staging / name)
                for name in sorted(p.name for p in staging.iterdir())
            },
        }
        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        _fsync_file(staging / MANIFEST_NAME)
        _fsync_dir(staging)

        if dest.exists():
            shutil.rmtree(dest)
        os.replace(staging, dest)
        _fsync_dir(dest.parent)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return dest


def read_manifest(path: Path) -> dict[str, Any]:
    manifest_path = Path(path) / MANIFEST_NAME
    if not manifest_path.exists():
        raise BundleCacheError(
            f"no bundle cache at {path}: build it once on the build box with "
            f"`python -m model.train_distilbert --build-cache --csv <jigsaw.csv> --cache {path}`. "
            "The training path never recomputes the 13.6-minute pipeline on a GPU pod."
        )
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != CACHE_FORMAT:
        raise BundleCacheError(
            f"bundle cache format {manifest.get('format')!r} != {CACHE_FORMAT!r}"
        )
    if manifest.get("labels") != list(LABELS):
        raise BundleCacheError(
            f"cache label order {manifest.get('labels')} != {list(LABELS)}; every per-label "
            "array in this project is positional, so a reordered cache mislabels silently"
        )
    return manifest


def load_bundle_cache(
    path: Path,
    *,
    expected_split_version: str | None = None,
    with_test: bool = False,
) -> CachedBundle:
    """Load the cache, verify every file digest, and refuse the wrong split.

    ``with_test`` defaults to False. The training path has no business holding the held-out
    rows in memory, and ``CachedBundle.test_df`` raises when it does not have them.
    """
    path = Path(path)
    manifest = read_manifest(path)

    if expected_split_version and manifest["split_version"] != expected_split_version:
        raise BundleCacheError(
            f"cache split_version {manifest['split_version'][:12]}... != requested "
            f"{expected_split_version[:12]}...; this cache is a different train/test/fold "
            "membership, so its numbers are not comparable with the classical model's"
        )

    for name, expected in manifest["files"].items():
        member = path / name
        if not member.exists():
            raise BundleCacheError(f"bundle cache is incomplete: {name} is missing")
        actual = sha256_bytes(member)
        if actual != expected:
            raise BundleCacheError(
                f"{name} digest {actual[:12]}... != manifest {expected[:12]}...; the cache has "
                "been modified or truncated since it was written"
            )

    train_df = _read_frame(path / TRAIN_FILE)
    if len(train_df) != manifest["n_train"]:
        raise BundleCacheError(
            f"train rows {len(train_df)} != manifest {manifest['n_train']}"
        )

    with np.load(path / FOLDS_FILE, allow_pickle=False) as npz:
        fold_indices = [
            (npz[f"fold{k}_train"], npz[f"fold{k}_val"]) for k in range(manifest["n_folds"])
        ]

    held_out = None
    if with_test:
        if not manifest.get("has_test", False):
            raise BundleCacheError(
                "this cache was written without the held-out rows (has_test=false); rebuild "
                "with include_test=True to evaluate on them"
            )
        held_out = _read_frame(path / TEST_FILE)

    return CachedBundle(
        train_df=train_df,
        fold_indices=fold_indices,
        raw_sha256=manifest["raw_sha256"],
        split_version=manifest["split_version"],
        env_version=manifest["env_version"],
        config=manifest["config"],
        path=path,
        held_out=held_out,
    )


def build_bundle_cache(csv_path: Path, dest: Path, *, seed: int = 42) -> Path:
    """The ONLY place ``prepare_dataset`` is reachable from. Run on the build box, once."""
    from model.data.firewall_check import assert_no_leakage  # noqa: PLC0415
    from model.data.prepare import SplitConfig, prepare_dataset  # noqa: PLC0415

    started = time.perf_counter()
    bundle = prepare_dataset(Path(csv_path), SplitConfig(seed=seed))
    assert_no_leakage(bundle)
    out = write_bundle_cache(bundle, Path(dest))
    print(
        f"cached bundle at {out} in {time.perf_counter() - started:.0f}s "
        f"train={len(bundle.train_df)} test={len(bundle.test_df)} "
        f"folds={len(bundle.fold_indices)} split_version={bundle.split_version}"
    )
    return out


# --------------------------------------------------------------------------------------
# Safe serialization
# --------------------------------------------------------------------------------------


def assert_safetensors_only(directory: Path) -> list[str]:
    """Raise if a pickle-format weight file is anywhere under ``directory``.

    ``save_safetensors=True`` is the default in the pinned transformers, which is exactly why
    this is checked rather than trusted: a default is one library bump away from changing, and
    a ``pytorch_model.bin`` in a registered artifact is arbitrary code execution at load time.
    """
    directory = Path(directory)
    offenders = sorted(
        str(p.relative_to(directory))
        for p in directory.rglob("*")
        if p.is_file() and p.suffix.lower() in _PICKLE_SUFFIXES
    )
    if offenders:
        raise UnsafeArtifact(
            f"pickle-format weight files under {directory}: {offenders}. "
            "safetensors only (delivery spec section 6.3); pass save_safetensors=True and "
            "safe_serialization=True, then delete the pickle."
        )
    weights = sorted(p.name for p in directory.rglob("*.safetensors"))
    if not weights:
        raise UnsafeArtifact(f"no .safetensors weight file under {directory}")
    return weights


# --------------------------------------------------------------------------------------
# Training configuration
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainConfig:
    model_name: str = MODEL_NAME
    max_length: int = MAX_LENGTH
    epochs: int = DEFAULT_EPOCHS
    learning_rate: float = DEFAULT_LR
    weight_decay: float = DEFAULT_WEIGHT_DECAY
    warmup_ratio: float = DEFAULT_WARMUP_RATIO
    batch_size: int = DEFAULT_BATCH_SIZE
    eval_batch_size: int = DEFAULT_EVAL_BATCH_SIZE
    gradient_accumulation_steps: int = 1
    early_stopping_patience: int = DEFAULT_PATIENCE
    early_stopping_threshold: float = 0.0
    metric_for_best_model: str = DEFAULT_BEST_METRIC
    train_probe_rows: int = DEFAULT_TRAIN_PROBE_ROWS
    fold: int = 0
    seed: int = 42
    fp16: bool = True
    max_train_rows: int | None = None
    labels: tuple[str, ...] = LABELS

    def __post_init__(self) -> None:
        if not MIN_EPOCHS <= self.epochs <= MAX_EPOCHS:
            raise ValueError(
                f"epochs={self.epochs} is outside the {MIN_EPOCHS}-{MAX_EPOCHS} the delivery "
                "spec allows; a 66M-parameter model on 180k short comments overfits past three"
            )
        if self.weight_decay <= 0:
            raise ValueError(
                "weight_decay must be > 0: it is one of the three named regularisers "
                "(early stopping, weight decay, few epochs) this fine-tune relies on"
            )
        if self.early_stopping_patience < 1:
            raise ValueError("early_stopping_patience must be >= 1; early stopping is required")

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["labels"] = list(self.labels)
        return out


# --------------------------------------------------------------------------------------
# Dataset and collation
# --------------------------------------------------------------------------------------


class MultiLabelDataset:
    """Pre-tokenized rows with float32 targets.

    float32 is not cosmetic. ``BCEWithLogitsLoss`` requires the target to have the same dtype
    as the logits; an int64 target raises deep inside the loss, and the "obvious" fix at 2 a.m.
    is to drop ``problem_type``, which is the wrong-objective bug this module exists to stop.
    """

    def __init__(self, encodings: dict[str, list], labels: np.ndarray) -> None:
        self.encodings = encodings
        self.labels = np.asarray(labels, dtype=np.float32)
        if self.labels.ndim != 2 or self.labels.shape[1] != len(LABELS):
            raise ValueError(f"labels must be (n, {len(LABELS)}), got {self.labels.shape}")
        if len(self.labels) != len(encodings["input_ids"]):
            raise ValueError(
                f"{len(encodings['input_ids'])} encodings against {len(self.labels)} labels"
            )

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, i: int) -> dict[str, Any]:
        item = {key: value[i] for key, value in self.encodings.items()}
        item["labels"] = self.labels[i].tolist()
        return item


class FloatLabelCollator:
    """Pad dynamically, then stack the targets as float32. Nothing else touches ``labels``."""

    def __init__(self, tokenizer) -> None:
        self.tokenizer = tokenizer

    def __call__(self, features: list[dict[str, Any]]) -> dict[str, Any]:
        import torch  # noqa: PLC0415

        labels = [feature["labels"] for feature in features]
        without_labels = [
            {key: value for key, value in feature.items() if key != "labels"}
            for feature in features
        ]
        batch = self.tokenizer.pad(without_labels, padding=True, return_tensors="pt")
        batch["labels"] = torch.tensor(np.asarray(labels, dtype=np.float32), dtype=torch.float32)
        return batch


def tokenize(tokenizer, texts: list[str], *, max_length: int = MAX_LENGTH) -> dict[str, list]:
    encoded = tokenizer(list(texts), truncation=True, max_length=max_length, padding=False)
    return {key: encoded[key] for key in ("input_ids", "attention_mask")}


def build_dataset(tokenizer, texts, labels, *, max_length: int = MAX_LENGTH) -> MultiLabelDataset:
    return MultiLabelDataset(tokenize(tokenizer, list(texts), max_length=max_length), labels)


# --------------------------------------------------------------------------------------
# The objective guard
# --------------------------------------------------------------------------------------


def build_model(config: TrainConfig, *, model_name: str | None = None):
    """``AutoModelForSequenceClassification`` with the multi-label objective pinned.

    ``id2label``/``label2id`` are set from ``LABELS`` in order, because ONNX export, the
    re-scorer and the API all read column ``j`` as ``LABELS[j]``. The head's column order is
    the one thing a later export cannot recover if it is wrong here.
    """
    from transformers import AutoModelForSequenceClassification  # noqa: PLC0415

    name = model_name or config.model_name
    model = AutoModelForSequenceClassification.from_pretrained(
        name,
        num_labels=len(LABELS),
        problem_type=PROBLEM_TYPE,
        id2label={i: label for i, label in enumerate(LABELS)},
        label2id={label: i for i, label in enumerate(LABELS)},
    )
    assert_multi_label_config(model.config)
    return model


def assert_multi_label_config(config) -> None:
    """Static half of the objective guard: config fields only, no forward pass."""
    if getattr(config, "problem_type", None) != PROBLEM_TYPE:
        raise ObjectiveError(
            f"problem_type is {getattr(config, 'problem_type', None)!r}, must be "
            f"{PROBLEM_TYPE!r}. Any other value puts a six-column target on softmax "
            "cross-entropy: the run does not crash, it trains the wrong objective."
        )
    if int(getattr(config, "num_labels", 0)) != len(LABELS):
        raise ObjectiveError(f"num_labels={config.num_labels}, must be {len(LABELS)}")
    id2label = getattr(config, "id2label", {}) or {}
    ordered = [id2label.get(i, id2label.get(str(i))) for i in range(len(LABELS))]
    if ordered != list(LABELS):
        raise ObjectiveError(
            f"id2label in index order is {ordered}, must be {list(LABELS)}; every per-label "
            "array downstream is positional, so a permuted head mislabels silently"
        )


def assert_bce_objective(model, *, atol: float = 1e-5) -> float:
    """Dynamic half: make the model compute a loss and check it against BCE by hand.

    A softmax head returns a completely different number here, so this fails on a synthetic
    batch in milliseconds rather than after a paid GPU hour. Returns the observed loss.
    """
    import torch  # noqa: PLC0415
    import torch.nn.functional as functional  # noqa: PLC0415

    assert_multi_label_config(model.config)
    was_training = model.training
    model.eval()
    generator = torch.Generator().manual_seed(0)
    vocab = int(getattr(model.config, "vocab_size", 30522))
    seq_len = 8
    input_ids = torch.randint(0, max(vocab - 1, 2), (4, seq_len), generator=generator)
    attention_mask = torch.ones_like(input_ids)
    labels = torch.tensor(
        [[1.0, 0.0, 1.0, 0.0, 0.0, 0.0],
         [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
         [1.0, 1.0, 1.0, 0.0, 1.0, 0.0],
         [0.0, 0.0, 0.0, 1.0, 0.0, 1.0]],
        dtype=torch.float32,
    )
    with torch.no_grad():
        out = model(input_ids=input_ids, attention_mask=attention_mask, labels=labels)
        expected = functional.binary_cross_entropy_with_logits(out.logits.float(), labels)
    if was_training:
        model.train()

    observed = float(out.loss)
    if out.logits.shape != labels.shape:
        raise ObjectiveError(f"logits {tuple(out.logits.shape)} != target {tuple(labels.shape)}")
    if abs(observed - float(expected)) > atol:
        raise ObjectiveError(
            f"the model's loss {observed:.6f} is not BCE-with-logits {float(expected):.6f} "
            f"(delta {abs(observed - float(expected)):.6f} > {atol}). That is softmax "
            "cross-entropy over six mutually exclusive classes on a corpus where labels "
            "co-occur; the objective is wrong and no metric downstream will say so."
        )
    return observed


# --------------------------------------------------------------------------------------
# Metrics and the per-epoch overfit probe
# --------------------------------------------------------------------------------------


def sigmoid(x) -> np.ndarray:
    """Element-wise logistic. Multi-label means sigmoid per column, never softmax.

    Written through ``logaddexp`` rather than ``1 / (1 + exp(-x))`` so a saturated logit
    underflows to 0.0 quietly instead of raising an overflow RuntimeWarning. Warnings that
    are always emitted are warnings nobody reads.
    """
    return np.exp(-np.logaddexp(0.0, -np.asarray(x, dtype=np.float64)))


def multi_label_metrics(logits, labels, *, threshold: float = 0.5) -> dict[str, float]:
    """Threshold-free PR-AUC plus a fixed-threshold F1 for orientation only.

    The 0.5 F1 here is diagnostic. Real thresholds are tuned out-of-fold by
    ``model/thresholds.py``; nothing is promoted on the numbers this function returns.
    """
    from sklearn.metrics import average_precision_score, f1_score  # noqa: PLC0415

    probs = sigmoid(logits)
    y_true = np.asarray(labels).astype(int)
    flags = (probs >= threshold).astype(int)

    out: dict[str, float] = {}
    pr_aucs, f1s = [], []
    for j, label in enumerate(LABELS):
        positives = int(y_true[:, j].sum())
        out[f"n_pos_{label}"] = float(positives)
        if positives == 0 or positives == len(y_true):
            # A degenerate column has no defined PR-AUC. Reporting 0.0 would drag the macro
            # down and reporting 1.0 would inflate it, so it is excluded and counted instead.
            out[f"pr_auc_{label}"] = float("nan")
            out[f"f1_{label}"] = float("nan")
            continue
        pr_auc = float(average_precision_score(y_true[:, j], probs[:, j]))
        f1 = float(f1_score(y_true[:, j], flags[:, j], zero_division=0))
        out[f"pr_auc_{label}"] = pr_auc
        out[f"f1_{label}"] = f1
        pr_aucs.append(pr_auc)
        f1s.append(f1)

    out["macro_pr_auc"] = float(np.mean(pr_aucs)) if pr_aucs else float("nan")
    out["macro_f1"] = float(np.mean(f1s)) if f1s else float("nan")
    out["n_labels_scored"] = float(len(pr_aucs))
    return out


def compute_metrics(eval_pred) -> dict[str, float]:
    logits = eval_pred.predictions
    if isinstance(logits, tuple):
        logits = logits[0]
    return multi_label_metrics(logits, eval_pred.label_ids)


def loss_gap(train_loss: float | None, val_loss: float | None) -> float | None:
    """``val - train``. Positive and growing is the overfit signature."""
    if train_loss is None or val_loss is None:
        return None
    return float(val_loss) - float(train_loss)


def make_epoch_gap_callback(train_probe, log_path: Path | None = None):
    """Log ``train_loss``, ``val_loss`` and their gap once per epoch, in eval mode both sides.

    Comparing Trainer's running training loss (dropout on, averaged over an epoch of moving
    weights) against a clean eval-mode validation loss understates the gap by exactly the
    amount that makes overfit invisible until it is expensive. This re-scores a fixed slice
    of the training rows the same way validation is scored.
    """
    from transformers import TrainerCallback  # noqa: PLC0415

    class EpochGapCallback(TrainerCallback):
        def __init__(self) -> None:
            self.trainer = None
            self._inside_probe = False
            # Turned off after trainer.train() returns. The final evaluate() on the restored
            # best checkpoint would otherwise append a duplicate epoch record and pay for a
            # second probe pass, and a duplicated last epoch is exactly the kind of detail
            # that makes an overfit curve unreadable.
            self.recording = True
            self.history: list[dict[str, float]] = []

        def _probe_metrics(self) -> dict[str, float]:
            """Score the training probe WITHOUT dispatching callbacks.

            ``Trainer.evaluate`` fires ``on_evaluate`` on every registered callback, so a
            probe run through it hands ``EarlyStoppingCallback`` a metrics dict whose keys
            are prefixed ``train_probe_``. It finds no ``eval_macro_pr_auc``, logs
            "early stopping is disabled", and returns. Measured on transformers 4.46.3.
            ``evaluation_loop`` is the same computation with no callback dispatch, so the
            overfit probe cannot interfere with the mechanism that decides when to stop.
            """
            if train_probe is None or self.trainer is None:
                return {}
            loader = self.trainer.get_eval_dataloader(train_probe)
            return self.trainer.evaluation_loop(
                loader, description="train probe", metric_key_prefix="train_probe"
            ).metrics

        def on_evaluate(self, args, state, control, metrics=None, **kwargs):
            if not self.recording or self._inside_probe:
                return
            if self.trainer is None or metrics is None:
                return
            if metrics.get("eval_loss") is None:
                return
            self._inside_probe = True
            try:
                probe = self._probe_metrics()
            finally:
                self._inside_probe = False

            train_loss = probe.get("train_probe_loss")
            val_loss = metrics.get("eval_loss")
            record = {
                "epoch": float(state.epoch or 0.0),
                "step": int(state.global_step),
                "train_probe_loss": None if train_loss is None else float(train_loss),
                "eval_loss": float(val_loss),
                "train_val_loss_gap": loss_gap(train_loss, val_loss),
                "eval_macro_pr_auc": metrics.get("eval_macro_pr_auc"),
                "eval_macro_f1": metrics.get("eval_macro_f1"),
                "train_probe_macro_pr_auc": probe.get("train_probe_macro_pr_auc"),
            }
            self.history.append(record)
            loggable = {k: v for k, v in record.items() if isinstance(v, int | float)}
            self.trainer.log(loggable)
            gap = record["train_val_loss_gap"]
            print(
                f"[epoch {record['epoch']:.2f}] train_probe_loss="
                f"{'n/a' if train_loss is None else f'{train_loss:.4f}'} "
                f"eval_loss={float(val_loss):.4f} "
                f"gap={'n/a' if gap is None else f'{gap:+.4f}'} "
                f"eval_macro_pr_auc={record['eval_macro_pr_auc']}",
                flush=True,
            )
            if log_path is not None:
                Path(log_path).write_text(json.dumps(self.history, indent=2) + "\n")

    return EpochGapCallback()


# --------------------------------------------------------------------------------------
# Trainer wiring
# --------------------------------------------------------------------------------------


def build_training_arguments(config: TrainConfig, output_dir: Path, *, report_to: list[str]):
    """TrainingArguments with early stopping, weight decay, and safetensors saving.

    ``eval_strategy`` was named ``evaluation_strategy`` before transformers 4.41 and the old
    name was removed in 4.46, so the parameter is chosen from the installed signature rather
    than from a version string.
    """
    import inspect  # noqa: PLC0415

    from transformers import TrainingArguments  # noqa: PLC0415

    kwargs: dict[str, Any] = {
        "output_dir": str(output_dir),
        "overwrite_output_dir": False,
        "num_train_epochs": config.epochs,
        "learning_rate": config.learning_rate,
        "weight_decay": config.weight_decay,
        "warmup_ratio": config.warmup_ratio,
        "per_device_train_batch_size": config.batch_size,
        "per_device_eval_batch_size": config.eval_batch_size,
        "gradient_accumulation_steps": config.gradient_accumulation_steps,
        "eval_strategy": "epoch",
        "save_strategy": "epoch",
        "save_total_limit": 2,
        "save_safetensors": True,
        "load_best_model_at_end": True,
        "metric_for_best_model": config.metric_for_best_model,
        "greater_is_better": not config.metric_for_best_model.endswith("loss"),
        "logging_strategy": "steps",
        "logging_steps": 50,
        "seed": config.seed,
        "data_seed": config.seed,
        # fp16 needs a CUDA device; asking for it on the build box is a hard error inside
        # TrainingArguments, and this file is exercised on CPU before it ever reaches a pod.
        "fp16": bool(config.fp16) and cuda_available(),
        "report_to": report_to,
        "label_names": ["labels"],
        "remove_unused_columns": False,
        "dataloader_num_workers": 2 if cuda_available() else 0,
    }
    parameters = set(inspect.signature(TrainingArguments.__init__).parameters)
    if "eval_strategy" not in parameters:
        kwargs["evaluation_strategy"] = kwargs.pop("eval_strategy")

    # Silently dropping an unknown kwarg is how a library bump quietly turns off weight decay
    # or safetensors, so the ones that carry a normative requirement are checked, not filtered.
    required = {
        "num_train_epochs", "weight_decay", "save_safetensors", "load_best_model_at_end",
        "metric_for_best_model", "label_names",
    }
    missing = sorted(key for key in required if key not in parameters)
    if missing:
        raise RuntimeError(
            f"the installed transformers does not accept {missing}; these carry normative "
            "requirements (epochs, weight decay, safetensors, early stopping) and cannot be "
            "dropped silently"
        )
    return TrainingArguments(**{k: v for k, v in kwargs.items() if k in parameters})


def cuda_available() -> bool:
    try:
        import torch  # noqa: PLC0415
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def build_trainer(model, tokenizer, args, train_ds, eval_ds, config: TrainConfig, gap_callback):
    from transformers import EarlyStoppingCallback, Trainer  # noqa: PLC0415

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=FloatLabelCollator(tokenizer),
        compute_metrics=compute_metrics,
        callbacks=[
            EarlyStoppingCallback(
                early_stopping_patience=config.early_stopping_patience,
                early_stopping_threshold=config.early_stopping_threshold,
            )
        ],
    )
    if gap_callback is not None:
        gap_callback.trainer = trainer
        trainer.add_callback(gap_callback)
    return trainer


def train(
    bundle: CachedBundle,
    config: TrainConfig,
    output_dir: Path,
    *,
    report_to: list[str] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Fine-tune one outer fold and write a safetensors model plus a training summary."""
    from transformers import AutoTokenizer  # noqa: PLC0415

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    set_all_seeds(config.seed)

    train_texts, y_train, val_texts, y_val = bundle.fold(config.fold)
    if config.max_train_rows is not None:
        train_texts = train_texts[: config.max_train_rows]
        y_train = y_train[: config.max_train_rows]

    tokenizer = AutoTokenizer.from_pretrained(config.model_name)
    train_ds = build_dataset(tokenizer, train_texts, y_train, max_length=config.max_length)
    eval_ds = build_dataset(tokenizer, val_texts, y_val, max_length=config.max_length)

    probe_rows = min(config.train_probe_rows, len(train_texts))
    train_probe = (
        build_dataset(
            tokenizer, train_texts[:probe_rows], y_train[:probe_rows], max_length=config.max_length
        )
        if probe_rows > 0
        else None
    )

    model = build_model(config)
    assert_bce_objective(model)

    args = build_training_arguments(config, output_dir, report_to=report_to or [])
    gap_callback = make_epoch_gap_callback(train_probe, output_dir / "epoch_gaps.json")
    trainer = build_trainer(model, tokenizer, args, train_ds, eval_ds, config, gap_callback)

    checkpoints = sorted(output_dir.glob("checkpoint-*"))
    started = time.perf_counter()
    trainer.train(resume_from_checkpoint=bool(resume and checkpoints))
    elapsed = time.perf_counter() - started

    if gap_callback is not None:
        gap_callback.recording = False

    final_dir = output_dir / "final"
    trainer.save_model(str(final_dir))
    tokenizer.save_pretrained(str(final_dir))
    # Trainer always writes training_args.bin, which is a torch.save pickle. It is not a
    # weight file and from_pretrained never reads it, but the artifact that goes to the
    # registry must contain no pickle at all, and every hyperparameter it holds is written
    # to training_summary.json below in JSON a human can read.
    (final_dir / "training_args.bin").unlink(missing_ok=True)
    weights = assert_safetensors_only(final_dir)

    eval_metrics = trainer.evaluate()
    # run_metadata carries raw_sha256 / split_version / env_version as OPTIONAL arguments that
    # default to None. Spreading its output over hand-written copies of the same three keys
    # silently blanked all three; they are passed in instead, so there is one source.
    meta = run_metadata(
        config.seed, bundle.raw_sha256, bundle.split_version, bundle.env_version
    )
    summary = {
        "model_name": config.model_name,
        "problem_type": PROBLEM_TYPE,
        "objective": "BCEWithLogits over six independent labels",
        "labels": list(LABELS),
        "fold": config.fold,
        "n_train": len(train_texts),
        "n_val": len(val_texts),
        "train_seconds": round(elapsed, 1),
        "hyperparameters": config.to_dict(),
        "epoch_gaps": gap_callback.history if gap_callback is not None else [],
        "final_val_metrics": {k: _json_safe(v) for k, v in eval_metrics.items()},
        "weights": weights,
        **meta,
        "data_version": bundle.data_version,
    }
    (final_dir / SUMMARY_NAME).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.floating | np.integer):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "DistilBERT multi-label fine-tune. Reads the cached Phase 0 bundle; never "
            "recomputes the split, and never touches the held-out test set."
        )
    )
    parser.add_argument("--cache", type=Path, default=Path("data/cache/bundle"))
    parser.add_argument("--build-cache", action="store_true",
                        help="build the bundle cache from --csv, then exit (build box only)")
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument("--expect-split-version", default=None,
                        help="refuse to train unless the cache carries this split_version")
    parser.add_argument("--output", type=Path, default=Path("artifacts/distilbert"))
    parser.add_argument("--fold", type=int, default=0)
    parser.add_argument("--model-name", default=MODEL_NAME)
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--lr", type=float, default=DEFAULT_LR)
    parser.add_argument("--weight-decay", type=float, default=DEFAULT_WEIGHT_DECAY)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--eval-batch-size", type=int, default=DEFAULT_EVAL_BATCH_SIZE)
    parser.add_argument("--grad-accum", type=int, default=1)
    parser.add_argument("--max-length", type=int, default=MAX_LENGTH)
    parser.add_argument("--patience", type=int, default=DEFAULT_PATIENCE)
    parser.add_argument("--best-metric", default=DEFAULT_BEST_METRIC)
    parser.add_argument("--train-probe-rows", type=int, default=DEFAULT_TRAIN_PROBE_ROWS)
    parser.add_argument("--max-train-rows", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--no-fp16", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-wandb", action="store_true")
    parser.add_argument("--wandb-project", default="mlops-toxic-moderation")
    parser.add_argument("--wandb-entity", default=os.environ.get("WANDB_ENTITY") or None)
    parser.add_argument("--hf-auth", action="store_true",
                        help="load HF_TOKEN from pass at point of use (public model: not needed)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.build_cache:
        if args.csv is None:
            raise SystemExit("--build-cache needs --csv")
        build_bundle_cache(args.csv, args.cache, seed=args.seed)
        return 0

    if args.hf_auth and not os.environ.get("HF_TOKEN"):
        os.environ["HF_TOKEN"] = load_secret("huggingface/token", "HF_TOKEN")

    bundle = load_bundle_cache(
        args.cache, expected_split_version=args.expect_split_version, with_test=False
    )
    config = TrainConfig(
        model_name=args.model_name,
        max_length=args.max_length,
        epochs=args.epochs,
        learning_rate=args.lr,
        weight_decay=args.weight_decay,
        batch_size=args.batch_size,
        eval_batch_size=args.eval_batch_size,
        gradient_accumulation_steps=args.grad_accum,
        early_stopping_patience=args.patience,
        metric_for_best_model=args.best_metric,
        train_probe_rows=args.train_probe_rows,
        fold=args.fold,
        seed=args.seed,
        fp16=not args.no_fp16,
        max_train_rows=args.max_train_rows,
    )

    run = None
    report_to: list[str] = []
    if not args.no_wandb:
        if not os.environ.get("WANDB_API_KEY"):
            os.environ["WANDB_API_KEY"] = load_secret("wandb/api-key", "WANDB_API_KEY")
        import wandb  # noqa: PLC0415

        # Only scalars and version hashes reach W&B. Raw comment text never leaves this box:
        # the project and the registry page are public by owner decision, which makes any
        # payload the last place a user comment could escape into a graded artifact.
        run = wandb.init(
            project=args.wandb_project,
            entity=args.wandb_entity,
            job_type="train-distilbert",
            config={
                **config.to_dict(),
                "raw_sha256": bundle.raw_sha256,
                "split_version": bundle.split_version,
                "env_version": bundle.env_version,
                "data_version": bundle.data_version,
                **run_metadata(config.seed),
            },
        )
        report_to = ["wandb"]

    try:
        summary = train(bundle, config, args.output, report_to=report_to, resume=args.resume)
    finally:
        if run is not None:
            run.finish()

    print(json.dumps(summary["final_val_metrics"], indent=2, sort_keys=True))
    print(f"model written to {args.output / 'final'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
