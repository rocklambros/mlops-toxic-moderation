"""Load and validate the raw Jigsaw CSV."""

from pathlib import Path

import pandas as pd

from model.labels import LABELS

REQUIRED_COLUMNS: tuple[str, ...] = ("id", "comment_text", *LABELS)


def load_raw(csv_path: Path) -> pd.DataFrame:
    # dtype=str on id: real Jigsaw ids are 16-hex strings, and pandas would silently
    # coerce an all-digit subset to int64, breaking the min(id) representative rule.
    df = pd.read_csv(csv_path, dtype={"id": str}, keep_default_na=False, na_values=[""])
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
    out = df[list(REQUIRED_COLUMNS)].copy()
    for label in LABELS:
        out[label] = out[label].astype(int)
    return out
