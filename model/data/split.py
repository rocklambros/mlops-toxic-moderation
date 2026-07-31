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

    msss = MultilabelStratifiedShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    train_idx, test_idx = next(msss.split(x, y))
    # np.sort: iterstrat returns indices in allocation order, so sorting makes the frame
    # row order a function of the data rather than of the library's internal traversal.
    train_df = df.iloc[np.sort(train_idx)].reset_index(drop=True)
    test_df = df.iloc[np.sort(test_idx)].reset_index(drop=True)

    y_train = train_df[list(LABELS)].to_numpy()
    mskf = MultilabelStratifiedKFold(n_splits=n_folds, shuffle=True, random_state=seed)
    fold_indices = [
        (tr, va) for tr, va in mskf.split(np.zeros((len(train_df), 1)), y_train)
    ]
    return train_df, test_df, fold_indices
