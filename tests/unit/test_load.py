from pathlib import Path

import pandas as pd
import pytest

from model.data.load import REQUIRED_COLUMNS, load_raw
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_load_raw_returns_required_columns():
    df = load_raw(FIXTURE)
    assert list(df.columns) == ["id", "comment_text", *LABELS]
    assert len(df) == 68


def test_load_raw_keeps_ids_as_strings():
    df = load_raw(FIXTURE)
    assert df["id"].map(type).eq(str).all()


def test_load_raw_rejects_missing_column(tmp_path):
    bad = tmp_path / "bad.csv"
    pd.DataFrame({"id": ["1"], "comment_text": ["hi"]}).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="missing columns"):
        load_raw(bad)


def test_load_raw_rejects_label_out_of_range(tmp_path):
    bad = tmp_path / "bad.csv"
    row = {"id": ["1"], "comment_text": ["hi"]}
    for label in LABELS:
        row[label] = [0]
    row["toxic"] = [2]
    pd.DataFrame(row).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="outside"):
        load_raw(bad)


def test_load_raw_rejects_null_comment_text(tmp_path):
    bad = tmp_path / "bad.csv"
    row = {"id": ["1"], "comment_text": [None]}
    for label in LABELS:
        row[label] = [0]
    pd.DataFrame(row).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="comment_text contains nulls"):
        load_raw(bad)


def test_required_columns_is_the_documented_tuple():
    assert REQUIRED_COLUMNS == ("id", "comment_text", *LABELS)
