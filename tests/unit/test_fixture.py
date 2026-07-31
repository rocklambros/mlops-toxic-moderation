from pathlib import Path

import pandas as pd

from model.data.dedup import dedup
from model.data.load import load_raw
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")
MIN_POSITIVES_AFTER_DEDUP = 9


def test_fixture_exists_and_has_the_documented_shape():
    df = pd.read_csv(FIXTURE)
    assert list(df.columns) == ["id", "comment_text", *LABELS]
    assert len(df) == 68


def test_every_label_has_slack_after_dedup():
    clean = dedup(load_raw(FIXTURE))
    assert len(clean) == 64
    for label in LABELS:
        assert clean[label].sum() >= MIN_POSITIVES_AFTER_DEDUP, (
            f"{label} has {int(clean[label].sum())} positives after dedup; the 15% test "
            f"split plus 5 folds needs at least {MIN_POSITIVES_AFTER_DEDUP}"
        )


def test_fixture_respects_the_label_hierarchy():
    df = load_raw(FIXTURE)
    assert int(((df["severe_toxic"] == 1) & (df["toxic"] == 0)).sum()) == 0
