"""Guards for the entrypoint that runs on the paid GPU pod.

The expensive failures this file exists to prevent all look like success while they happen:
a softmax objective that trains happily on a six-column target, a pod that recomputes a
different split from the one the classical model used, a held-out test set read by the
training path, and a `pytorch_model.bin` in a registered artifact.

torch and transformers are imported lazily by the module under test, so the cache, config
and metric guards run everywhere. The cases that need a real model are skipped where the
GPU stack is absent.
"""

import json

import numpy as np
import pandas as pd
import pytest

from model.data.prepare import DatasetBundle, SplitConfig
from model.labels import LABELS
from model.train_distilbert import (
    MAX_EPOCHS,
    PROBLEM_TYPE,
    BundleCacheError,
    CachedBundle,
    HeldOutTestAccess,
    MultiLabelDataset,
    ObjectiveError,
    TrainConfig,
    UnsafeArtifact,
    assert_multi_label_config,
    assert_safetensors_only,
    load_bundle_cache,
    load_secret,
    loss_gap,
    multi_label_metrics,
    read_manifest,
    sigmoid,
    write_bundle_cache,
)

CLEAN = "thanks for the edit"
CUES = {
    "toxic": "idiot", "severe_toxic": "vile", "obscene": "filth",
    "threat": "killyou", "insult": "moron", "identity_hate": "yourkind",
}


def _frame(n, *, seed=0, start=0):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        y = {label: int(rng.random() < 0.35) for label in LABELS}
        y["severe_toxic"] = y["severe_toxic"] & y["toxic"]
        parts = [CLEAN, f"row {start + i}"] + [CUES[la] for la in LABELS if y[la]]
        rows.append({"id": f"{start + i:016x}", "comment_text": " ".join(parts), **y})
    return pd.DataFrame(rows)


def _bundle(n_train=40, n_test=10, n_folds=2):
    train_df, test_df = _frame(n_train, seed=1), _frame(n_test, seed=2, start=1000)
    idx = np.arange(n_train)
    folds = [
        (idx[idx % n_folds != k].copy(), idx[idx % n_folds == k].copy()) for k in range(n_folds)
    ]
    return DatasetBundle(
        train_df=train_df,
        test_df=test_df,
        fold_indices=folds,
        raw_sha256="a" * 64,
        split_version="b" * 64,
        env_version="c" * 64,
        config=SplitConfig(seed=42, test_size=0.15, n_folds=n_folds),
    )


# ---------------------------------------------------------------------------------------
# The cache: the pod must never recompute the split
# ---------------------------------------------------------------------------------------


def test_the_cache_round_trips_the_frame_the_folds_and_all_three_version_fields(tmp_path):
    bundle = _bundle()
    path = write_bundle_cache(bundle, tmp_path / "bundle")
    cached = load_bundle_cache(path)

    pd.testing.assert_frame_equal(cached.train_df, bundle.train_df)
    assert cached.raw_sha256 == bundle.raw_sha256
    assert cached.split_version == bundle.split_version
    assert cached.env_version == bundle.env_version
    assert cached.data_version == bundle.data_version
    assert len(cached.fold_indices) == len(bundle.fold_indices)
    for (want_tr, want_va), (got_tr, got_va) in zip(
        bundle.fold_indices, cached.fold_indices, strict=True
    ):
        assert np.array_equal(want_tr, got_tr) and np.array_equal(want_va, got_va)


def test_the_cache_is_a_directory_of_open_formats_with_no_pickle(tmp_path):
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    names = sorted(p.name for p in path.iterdir())
    assert names == ["folds.npz", "manifest.json", "test.csv.gz", "train.csv.gz"]
    assert not any(p.suffix in (".pkl", ".pickle", ".joblib", ".bin") for p in path.iterdir())


def test_the_held_out_rows_are_not_reachable_from_the_training_path(tmp_path):
    """DistilBERT is evaluated on validation folds. Nothing here may read the test set."""
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    cached = load_bundle_cache(path)  # with_test defaults to False
    with pytest.raises(HeldOutTestAccess, match="selection on the test set"):
        _ = cached.test_df

    with_test = load_bundle_cache(path, with_test=True)
    assert len(with_test.test_df) == 10


def test_a_cache_written_for_a_pod_can_omit_the_held_out_rows_entirely(tmp_path):
    path = write_bundle_cache(_bundle(), tmp_path / "bundle", include_test=False)
    assert not (path / "test.csv.gz").exists()
    assert read_manifest(path)["has_test"] is False
    load_bundle_cache(path)  # training path is unaffected
    with pytest.raises(BundleCacheError, match="has_test=false"):
        load_bundle_cache(path, with_test=True)


def test_a_modified_cache_file_is_refused(tmp_path):
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    frame = pd.read_csv(path / "train.csv.gz", dtype={"id": str}, keep_default_na=False)
    frame.loc[0, "toxic"] = 1 - int(frame.loc[0, "toxic"])
    frame.to_csv(path / "train.csv.gz", index=False, compression="gzip")
    with pytest.raises(BundleCacheError, match="modified or truncated"):
        load_bundle_cache(path)


def test_a_truncated_cache_is_refused(tmp_path):
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    (path / "folds.npz").write_bytes(b"")
    with pytest.raises(BundleCacheError, match="digest"):
        load_bundle_cache(path)


def test_a_missing_cache_names_the_command_that_builds_it(tmp_path):
    with pytest.raises(BundleCacheError, match=r"--build-cache"):
        load_bundle_cache(tmp_path / "absent")


def test_a_cache_for_a_different_split_is_refused_before_any_training_starts(tmp_path):
    """Two pods on two splits produce two numbers that were never comparable."""
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    load_bundle_cache(path, expected_split_version="b" * 64)
    with pytest.raises(BundleCacheError, match="different train/test/fold membership"):
        load_bundle_cache(path, expected_split_version="d" * 64)


def test_a_cache_whose_label_order_differs_is_refused(tmp_path):
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["labels"] = sorted(LABELS)
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BundleCacheError, match="label order"):
        load_bundle_cache(path)


def test_an_unknown_cache_format_is_refused_rather_than_guessed(tmp_path):
    path = write_bundle_cache(_bundle(), tmp_path / "bundle")
    manifest = json.loads((path / "manifest.json").read_text())
    manifest["format"] = "somebody-elses-cache/9"
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BundleCacheError, match="format"):
        read_manifest(path)


def test_a_crash_during_the_write_leaves_the_previous_cache_intact(tmp_path, monkeypatch):
    """The manifest is written last and the directory is moved into place with os.replace."""
    from model import train_distilbert as module

    dest = tmp_path / "bundle"
    write_bundle_cache(_bundle(), dest)
    good = json.loads((dest / "manifest.json").read_text())

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_write_frame", explode)
    with pytest.raises(OSError):
        write_bundle_cache(_bundle(n_train=8, n_test=2), dest)

    assert json.loads((dest / "manifest.json").read_text()) == good
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".bundle.tmp")]


def test_gzip_bytes_are_a_function_of_the_data_not_of_the_clock(tmp_path):
    """A rebuilt cache with the same split has the same digest, so a changed digest means data."""
    first = write_bundle_cache(_bundle(), tmp_path / "one")
    second = write_bundle_cache(_bundle(), tmp_path / "two")
    assert (first / "train.csv.gz").read_bytes() == (second / "train.csv.gz").read_bytes()
    assert read_manifest(first)["files"] == read_manifest(second)["files"]


# ---------------------------------------------------------------------------------------
# Folds
# ---------------------------------------------------------------------------------------


def test_a_fold_yields_disjoint_rows_and_float32_targets(tmp_path):
    cached = load_bundle_cache(write_bundle_cache(_bundle(), tmp_path / "bundle"))
    train_texts, y_train, val_texts, y_val = cached.fold(0)

    assert len(train_texts) + len(val_texts) == len(cached.train_df)
    assert set(train_texts).isdisjoint(set(val_texts))
    assert y_train.dtype == np.float32, "BCEWithLogitsLoss needs a float target, not int64"
    assert y_train.shape[1] == len(LABELS) and y_val.shape[1] == len(LABELS)


def test_an_out_of_range_fold_is_a_hard_error(tmp_path):
    cached = load_bundle_cache(write_bundle_cache(_bundle(), tmp_path / "bundle"))
    with pytest.raises(ValueError, match="out of range"):
        cached.fold(9)


def test_overlapping_fold_indices_are_refused(tmp_path):
    idx = np.arange(10)
    cached = CachedBundle(
        train_df=_frame(10),
        fold_indices=[(idx, idx)],
        raw_sha256="a" * 64, split_version="b" * 64, env_version="c" * 64,
        config={"seed": 42}, path=tmp_path,
    )
    with pytest.raises(BundleCacheError, match="both the fit and the validation set"):
        cached.fold(0)


# ---------------------------------------------------------------------------------------
# Safe serialization
# ---------------------------------------------------------------------------------------


def test_safetensors_only_accepts_a_safetensors_directory(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"x")
    (tmp_path / "config.json").write_text("{}")
    assert assert_safetensors_only(tmp_path) == ["model.safetensors"]


def test_a_pickle_weight_file_blocks_the_artifact(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"x")
    (tmp_path / "pytorch_model.bin").write_bytes(b"x")
    with pytest.raises(UnsafeArtifact, match="pytorch_model.bin"):
        assert_safetensors_only(tmp_path)


def test_a_pickle_hidden_in_a_checkpoint_subdirectory_is_still_found(tmp_path):
    (tmp_path / "model.safetensors").write_bytes(b"x")
    (tmp_path / "checkpoint-2").mkdir()
    (tmp_path / "checkpoint-2" / "optimizer.pt").write_bytes(b"x")
    with pytest.raises(UnsafeArtifact, match="optimizer.pt"):
        assert_safetensors_only(tmp_path)


def test_a_directory_with_no_weights_at_all_is_refused(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(UnsafeArtifact, match="no .safetensors"):
        assert_safetensors_only(tmp_path)


# ---------------------------------------------------------------------------------------
# Configuration invariants
# ---------------------------------------------------------------------------------------


def test_the_epoch_budget_is_enforced_not_documented():
    TrainConfig(epochs=2)
    TrainConfig(epochs=MAX_EPOCHS)
    with pytest.raises(ValueError, match="outside the 1-3"):
        TrainConfig(epochs=MAX_EPOCHS + 1)
    with pytest.raises(ValueError, match="outside the 1-3"):
        TrainConfig(epochs=0)


def test_weight_decay_cannot_be_switched_off():
    with pytest.raises(ValueError, match="weight_decay"):
        TrainConfig(weight_decay=0.0)


def test_early_stopping_cannot_be_switched_off():
    with pytest.raises(ValueError, match="early_stopping_patience"):
        TrainConfig(early_stopping_patience=0)


def test_the_default_selection_metric_is_threshold_free():
    """Thresholds are tuned out-of-fold AFTER training; selecting on one now is circular."""
    assert TrainConfig().metric_for_best_model == "eval_macro_pr_auc"


def test_the_config_serializes_for_the_run_page():
    payload = json.loads(json.dumps(TrainConfig().to_dict()))
    assert payload["labels"] == list(LABELS)
    assert payload["weight_decay"] > 0
    assert payload["epochs"] <= MAX_EPOCHS


# ---------------------------------------------------------------------------------------
# Objective guard, dataset, metrics
# ---------------------------------------------------------------------------------------


def test_the_static_objective_guard_rejects_a_single_label_config():
    class _Config:
        problem_type = "single_label_classification"
        num_labels = len(LABELS)
        id2label = dict(enumerate(LABELS))

    with pytest.raises(ObjectiveError, match="trains the wrong objective"):
        assert_multi_label_config(_Config())


def test_the_static_objective_guard_rejects_an_unset_problem_type():
    class _Config:
        problem_type = None
        num_labels = len(LABELS)
        id2label = dict(enumerate(LABELS))

    with pytest.raises(ObjectiveError, match=PROBLEM_TYPE):
        assert_multi_label_config(_Config())


def test_the_static_objective_guard_rejects_a_permuted_head():
    class _Config:
        problem_type = PROBLEM_TYPE
        num_labels = len(LABELS)
        id2label = dict(enumerate(sorted(LABELS)))

    with pytest.raises(ObjectiveError, match="positional"):
        assert_multi_label_config(_Config())


def test_the_static_objective_guard_rejects_a_wrong_head_width():
    class _Config:
        problem_type = PROBLEM_TYPE
        num_labels = 2
        id2label = dict(enumerate(LABELS))

    with pytest.raises(ObjectiveError, match="num_labels"):
        assert_multi_label_config(_Config())


def test_the_dataset_carries_float_targets_and_checks_its_own_shape():
    encodings = {"input_ids": [[1, 2], [3, 4]], "attention_mask": [[1, 1], [1, 1]]}
    dataset = MultiLabelDataset(encodings, np.ones((2, len(LABELS)), dtype=int))
    assert len(dataset) == 2
    item = dataset[0]
    assert item["input_ids"] == [1, 2]
    assert item["labels"] == [1.0] * len(LABELS)
    assert all(isinstance(v, float) for v in item["labels"])

    with pytest.raises(ValueError, match=r"\(n, 6\)"):
        MultiLabelDataset(encodings, np.ones((2, 3)))
    with pytest.raises(ValueError, match="against"):
        MultiLabelDataset(encodings, np.ones((3, len(LABELS))))


def test_sigmoid_is_stable_at_the_extremes():
    values = sigmoid(np.array([-800.0, 0.0, 800.0]))
    assert np.isfinite(values).all()
    assert values[0] == pytest.approx(0.0)
    assert values[1] == pytest.approx(0.5)
    assert values[2] == pytest.approx(1.0)


def test_metrics_report_pr_auc_per_label_and_a_macro_over_the_scorable_ones():
    rng = np.random.default_rng(0)
    y = (rng.random((200, len(LABELS))) < 0.3).astype(int)
    logits = np.where(y == 1, 2.0, -2.0) + rng.normal(0, 0.5, y.shape)
    out = multi_label_metrics(logits, y)
    assert out["macro_pr_auc"] > 0.9
    assert out["n_labels_scored"] == len(LABELS)
    assert out["pr_auc_threat"] > 0.5
    assert out["n_pos_toxic"] == float(y[:, 0].sum())


def test_a_degenerate_label_is_excluded_from_the_macro_rather_than_scored_zero():
    """An all-negative column has no defined PR-AUC; scoring it 0.0 would drag the macro down."""
    y = np.zeros((50, len(LABELS)), dtype=int)
    y[:20, 0] = 1
    logits = np.where(y == 1, 2.0, -2.0)
    out = multi_label_metrics(logits, y)
    assert np.isnan(out["pr_auc_threat"])
    assert out["n_labels_scored"] == 1
    assert out["macro_pr_auc"] == pytest.approx(1.0)


def test_the_loss_gap_is_validation_minus_train_and_tolerates_a_missing_side():
    assert loss_gap(0.10, 0.35) == pytest.approx(0.25)
    assert loss_gap(0.35, 0.10) == pytest.approx(-0.25)
    assert loss_gap(None, 0.2) is None
    assert loss_gap(0.2, None) is None


# ---------------------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------------------


def test_a_secret_comes_from_the_environment_first_and_never_from_argv(monkeypatch):
    called = []
    monkeypatch.setenv("WANDB_API_KEY", "from-env")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: called.append(a) or pytest.fail("must not shell out")
    )
    assert load_secret("wandb/api-key", "WANDB_API_KEY") == "from-env"
    assert called == []


def test_a_failed_pass_lookup_raises_without_echoing_the_secret(monkeypatch):
    import subprocess as sp

    monkeypatch.delenv("WANDB_API_KEY", raising=False)

    def fail(*_a, **kwargs):
        assert kwargs.get("timeout") == 5, "the pass subprocess must carry a 5s timeout"
        raise sp.CalledProcessError(2, ["pass", "show", "wandb/api-key"], output="s3cr3t")

    monkeypatch.setattr(sp, "run", fail)
    with pytest.raises(RuntimeError) as excinfo:
        load_secret("wandb/api-key", "WANDB_API_KEY")
    assert "s3cr3t" not in str(excinfo.value)
    assert "wandb/api-key" in str(excinfo.value)


def test_a_hanging_pass_lookup_does_not_hang_the_pod(monkeypatch):
    import subprocess as sp

    monkeypatch.delenv("WANDB_API_KEY", raising=False)

    def timeout(*_a, **_k):
        raise sp.TimeoutExpired(["pass"], 5)

    monkeypatch.setattr(sp, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out"):
        load_secret("wandb/api-key", "WANDB_API_KEY")


# ---------------------------------------------------------------------------------------
# With a real (tiny) transformers model
# ---------------------------------------------------------------------------------------

VOCAB = [
    "[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]",
    "thanks", "for", "the", "edit", "row", "idiot", "vile", "filth", "killyou",
    "moron", "yourkind", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
]


def _tiny_pretrained(tmp_path, *, problem_type=PROBLEM_TYPE, id2label=None):
    from transformers import (
        DistilBertConfig,
        DistilBertForSequenceClassification,
        DistilBertTokenizerFast,
    )

    model_dir = tmp_path / f"tiny-{problem_type}"
    model_dir.mkdir(parents=True, exist_ok=True)
    vocab_file = tmp_path / "vocab.txt"
    vocab_file.write_text("\n".join(VOCAB) + "\n")
    config = DistilBertConfig(
        vocab_size=len(VOCAB), dim=32, n_layers=2, n_heads=2, hidden_dim=64,
        max_position_embeddings=64, num_labels=len(LABELS), problem_type=problem_type,
        id2label=id2label or {i: la for i, la in enumerate(LABELS)},
        label2id={la: i for i, la in enumerate(id2label.values() if id2label else LABELS)},
    )
    DistilBertForSequenceClassification(config).save_pretrained(
        str(model_dir), safe_serialization=True
    )
    DistilBertTokenizerFast(vocab_file=str(vocab_file)).save_pretrained(str(model_dir))
    return model_dir


def test_the_model_factory_pins_the_multi_label_objective(tmp_path):
    pytest.importorskip("torch")
    from model.train_distilbert import assert_bce_objective, build_model

    model_dir = _tiny_pretrained(tmp_path, problem_type="single_label_classification")
    model = build_model(TrainConfig(), model_name=str(model_dir))

    assert model.config.problem_type == PROBLEM_TYPE, (
        "the factory must override a checkpoint's single-label problem_type"
    )
    assert [model.config.id2label[i] for i in range(len(LABELS))] == list(LABELS)
    assert_bce_objective(model)


def test_the_dynamic_guard_catches_a_softmax_objective_on_a_six_column_target(tmp_path):
    """The wrong-objective bug, reproduced. Nothing about this run would otherwise look ill."""
    pytest.importorskip("torch")
    import torch
    from transformers import AutoModelForSequenceClassification

    from model.train_distilbert import assert_bce_objective

    model_dir = _tiny_pretrained(tmp_path)
    model = AutoModelForSequenceClassification.from_pretrained(str(model_dir))
    model.config.problem_type = "single_label_classification"

    # It trains: a six-column float target goes through CrossEntropyLoss without complaint.
    logits = torch.zeros((2, len(LABELS)))
    target = torch.tensor([[1.0, 0, 1, 0, 0, 0], [0, 0, 0, 1.0, 0, 0]])
    assert torch.isfinite(torch.nn.functional.cross_entropy(logits, target))

    with pytest.raises(ObjectiveError, match="softmax cross-entropy"):
        assert_bce_objective(model)


def test_the_dynamic_guard_passes_only_when_the_loss_really_is_bce(tmp_path):
    pytest.importorskip("torch")
    from model.train_distilbert import assert_bce_objective

    model = _load(_tiny_pretrained(tmp_path))
    observed = assert_bce_objective(model)
    assert 0.0 < observed < 5.0


def _load(model_dir):
    from transformers import AutoModelForSequenceClassification

    return AutoModelForSequenceClassification.from_pretrained(str(model_dir))


def test_the_collator_pads_dynamically_and_emits_float32_targets(tmp_path):
    pytest.importorskip("torch")
    import torch
    from transformers import AutoTokenizer

    from model.train_distilbert import FloatLabelCollator, build_dataset

    tokenizer = AutoTokenizer.from_pretrained(str(_tiny_pretrained(tmp_path)))
    dataset = build_dataset(
        tokenizer,
        ["idiot", "thanks for the edit idiot moron"],
        np.array([[1, 0, 0, 0, 0, 0], [1, 0, 0, 0, 1, 0]]),
        max_length=16,
    )
    batch = FloatLabelCollator(tokenizer)([dataset[0], dataset[1]])

    assert batch["labels"].dtype == torch.float32
    assert batch["labels"].shape == (2, len(LABELS))
    assert batch["input_ids"].shape[0] == 2
    assert batch["input_ids"].shape[1] == batch["attention_mask"].shape[1]


def test_the_training_arguments_carry_every_normative_requirement(tmp_path):
    pytest.importorskip("transformers")
    from model.train_distilbert import build_training_arguments

    args = build_training_arguments(TrainConfig(epochs=2), tmp_path, report_to=[])
    assert args.num_train_epochs == 2
    assert args.weight_decay > 0
    assert args.save_safetensors is True
    assert args.load_best_model_at_end is True
    assert args.metric_for_best_model == "eval_macro_pr_auc"
    assert args.greater_is_better is True
    assert args.label_names == ["labels"]
    assert getattr(args, "eval_strategy", getattr(args, "evaluation_strategy", None)) == "epoch"
    assert args.fp16 is False, "fp16 must be off where there is no CUDA device"


def test_a_full_micro_fine_tune_writes_safetensors_and_a_per_epoch_gap(tmp_path, caplog):
    """One epoch, forty rows, a two-layer model: the whole pod path exercised on CPU."""
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    import logging

    from transformers.trainer_callback import logger as callback_logger

    from model.train_distilbert import train

    # transformers detaches its loggers from the root, so caplog only sees this if the
    # handler is attached directly. Without it the "early stopping is disabled" regression
    # is invisible to the suite, which is how it survived the first run.
    callback_logger.addHandler(caplog.handler)
    callback_logger.propagate = False
    caplog.set_level(logging.WARNING)

    cache = write_bundle_cache(_bundle(n_train=40, n_test=10), tmp_path / "bundle")
    bundle = load_bundle_cache(cache)
    config = TrainConfig(
        model_name=str(_tiny_pretrained(tmp_path)),
        max_length=16, epochs=2, batch_size=8, eval_batch_size=8,
        train_probe_rows=8, fp16=False, fold=0,
    )
    summary = train(bundle, config, tmp_path / "out", report_to=[])

    final = tmp_path / "out" / "final"
    assert (final / "model.safetensors").exists()
    assert not list(final.glob("*.bin")), "training_args.bin is a pickle; it must not ship"
    assert "early stopping is disabled" not in caplog.text, (
        "the per-epoch overfit probe must not be dispatched to EarlyStoppingCallback: it "
        "carries train_probe_ keys, finds no eval_ metric, and turns early stopping off"
    )
    assert summary["problem_type"] == PROBLEM_TYPE
    assert summary["labels"] == list(LABELS)
    assert summary["split_version"] == "b" * 64
    assert summary["n_train"] + summary["n_val"] == 40

    gaps = json.loads((tmp_path / "out" / "epoch_gaps.json").read_text())
    assert [g["epoch"] for g in gaps] == [1.0, 2.0], (
        "one record per epoch and no more: the final evaluate() on the restored best "
        "checkpoint must not append a duplicate last epoch to the overfit curve"
    )
    record = gaps[0]
    assert record["train_probe_loss"] is not None
    assert record["eval_loss"] is not None
    assert record["train_val_loss_gap"] == pytest.approx(
        record["eval_loss"] - record["train_probe_loss"]
    )
    assert json.loads((final / "training_summary.json").read_text())["fold"] == 0
