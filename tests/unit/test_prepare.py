from pathlib import Path

import pandas as pd

from model.data.load import REQUIRED_COLUMNS
from model.data.prepare import (
    SplitConfig,
    compute_env_version,
    label_fingerprint,
    prepare_dataset,
)
from model.labels import LABELS

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_prepare_is_deterministic_across_all_three_version_fields():
    a = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    b = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    assert (a.raw_sha256, a.split_version, a.env_version) == (
        b.raw_sha256, b.split_version, b.env_version)
    assert a.data_version == b.data_version
    assert list(a.test_df["id"]) == list(b.test_df["id"])


def test_seed_moves_split_version_only():
    a = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    b = prepare_dataset(FIXTURE, SplitConfig(seed=7))
    assert a.split_version != b.split_version
    assert a.raw_sha256 == b.raw_sha256
    assert a.env_version == b.env_version


def test_raw_sha256_is_the_digest_of_the_file_on_disk():
    import hashlib
    bundle = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    assert bundle.raw_sha256 == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()


def test_relabelling_moves_raw_sha256_and_split_version(tmp_path):
    a = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    df = pd.read_csv(FIXTURE, dtype={"id": str})
    df.loc[0, "toxic"] = 1 - int(df.loc[0, "toxic"])
    relabeled = tmp_path / "relabeled.csv"
    df[list(REQUIRED_COLUMNS)].to_csv(relabeled, index=False)
    b = prepare_dataset(relabeled, SplitConfig(seed=42))
    assert a.raw_sha256 != b.raw_sha256
    assert a.split_version != b.split_version
    assert a.env_version == b.env_version


def test_env_version_tracks_dedup_parameters(monkeypatch):
    before = compute_env_version()
    monkeypatch.setattr("model.data.prepare.DEDUP_JACCARD", 0.75)
    assert compute_env_version() != before


def test_label_fingerprint_is_vectorized_and_order_independent():
    df = pd.DataFrame(
        {"id": ["b", "a"], **{lb: [1, 0] for lb in LABELS}}
    )
    reordered = df.iloc[::-1].reset_index(drop=True)
    assert label_fingerprint(df) == label_fingerprint(reordered)
    assert label_fingerprint(df) == ["a:000000", "b:111111"]


def test_prepare_uses_the_documented_default_config():
    import inspect
    default = inspect.signature(prepare_dataset).parameters["config"].default
    assert default == SplitConfig(seed=42, test_size=0.15, n_folds=5)


def test_bundle_comparison_does_not_raise_on_dataframes():
    a = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    b = prepare_dataset(FIXTURE, SplitConfig(seed=7))
    assert a == a
    assert a != b
    assert hash(a) == hash(a)
