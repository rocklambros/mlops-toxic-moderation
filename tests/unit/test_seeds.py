import sys

import numpy as np

from model.seeds import assert_hash_seed_pinned, run_metadata, set_all_seeds


def test_set_all_seeds_makes_numpy_deterministic():
    set_all_seeds(123)
    a = np.random.rand(5)
    set_all_seeds(123)
    assert np.array_equal(a, np.random.rand(5))


def test_run_metadata_carries_all_three_version_fields():
    meta = run_metadata(seed=7, raw_sha256="raw", split_version="split", env_version="env")
    assert meta["git_sha"]
    assert meta["seed"] == 7
    assert meta["raw_sha256"] == "raw"
    assert meta["split_version"] == "split"
    assert meta["env_version"] == "env"
    assert meta["hash_randomization"] is False
    assert "timestamp_utc" in meta


def test_assert_hash_seed_pinned_passes_under_the_pinned_suite():
    assert sys.flags.hash_randomization == 0
    assert_hash_seed_pinned()
