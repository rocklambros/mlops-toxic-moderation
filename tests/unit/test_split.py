from pathlib import Path

import pytest

from model.data.dedup import dedup
from model.data.load import load_raw
from model.data.split import make_splits
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")
SEEDS = [0, 7, 42, 123, 2024]


def _clean():
    return dedup(load_raw(FIXTURE))


@pytest.mark.parametrize("seed", SEEDS)
def test_test_set_is_disjoint_from_train(seed):
    train_df, test_df, _ = make_splits(_clean(), seed=seed, n_folds=5)
    assert set(train_df["id"]).isdisjoint(set(test_df["id"]))
    assert len(train_df) + len(test_df) == len(_clean())


@pytest.mark.parametrize("seed", SEEDS)
def test_every_label_present_in_test_and_every_fold(seed):
    train_df, test_df, folds = make_splits(_clean(), seed=seed, n_folds=5)
    for label in LABELS:
        assert test_df[label].sum() >= 1, f"{label} missing from test at seed {seed}"
    ytr = train_df[list(LABELS)].to_numpy()
    for i, (_, val_idx) in enumerate(folds):
        positives = ytr[val_idx].sum(axis=0)
        assert (positives >= 1).all(), f"label missing from fold {i} at seed {seed}"


@pytest.mark.parametrize("seed", SEEDS)
def test_fixture_carries_slack_above_the_one_positive_per_fold_minimum(seed):
    train_df, _, folds = make_splits(_clean(), seed=seed, n_folds=5)
    ytr = train_df[list(LABELS)].to_numpy()
    for _, val_idx in folds:
        assert ytr[val_idx].sum(axis=0).min() >= 2


@pytest.mark.parametrize("seed", SEEDS)
def test_split_is_deterministic_for_fixed_seed(seed):
    a_train, a_test, a_folds = make_splits(_clean(), seed=seed, n_folds=5)
    b_train, b_test, b_folds = make_splits(_clean(), seed=seed, n_folds=5)
    assert list(a_test["id"]) == list(b_test["id"])
    assert list(a_train["id"]) == list(b_train["id"])
    for (_, a_val), (_, b_val) in zip(a_folds, b_folds, strict=True):
        assert list(a_val) == list(b_val)


def test_test_size_is_about_fifteen_percent():
    clean = _clean()
    _, test_df, _ = make_splits(clean, seed=42, n_folds=5)
    assert 0.10 <= len(test_df) / len(clean) <= 0.20
