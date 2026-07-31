from pathlib import Path

import pandas as pd
import pytest

from model.data.dedup import DEDUP_JACCARD, LSH_BANDS, LSH_ROWS, dedup, lsh_recall
from model.data.firewall_check import (
    GATE_BANDS,
    GATE_JACCARD,
    GATE_ROWS,
    assert_no_leakage,
    gate_recall,
    leakage_report,
)
from model.data.prepare import DatasetBundle, SplitConfig, prepare_dataset
from model.data.shingles import cache_stats, clear_cache, jaccard, shingle_set
from model.normalize import normalize

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def _bundle(seed: int = 42) -> DatasetBundle:
    return prepare_dataset(FIXTURE, SplitConfig(seed=seed))


def _replace_test(bundle: DatasetBundle, test_df: pd.DataFrame) -> DatasetBundle:
    return DatasetBundle(
        train_df=bundle.train_df,
        test_df=test_df,
        fold_indices=bundle.fold_indices,
        raw_sha256=bundle.raw_sha256,
        split_version=bundle.split_version,
        env_version=bundle.env_version,
        config=bundle.config,
    )




def test_gate_is_independent_of_dedup_by_banding_not_by_threshold():
    """Independence used to be bought with a LOWER gate threshold. That is unsatisfiable
    on a corpus with a continuum of similarities: the band between the two thresholds is
    never empty, so the gate can never pass. Measured on Jigsaw, 8,711 cross-split pairs
    sat in [0.70, 0.80) -- 564 distinct test rows, 1.75% of the held-out set.

    Independence now comes from the METHOD. Both stages agree what a duplicate is, they
    block with different bandings, and the decision in both is exact Jaccard. The gate can
    still catch a pair dedup's blocking failed to compare, which is the ~1% residue a
    0.99-recall blocker leaves behind.
    """
    assert GATE_JACCARD == DEDUP_JACCARD, "both stages must agree what a duplicate is"
    assert (GATE_BANDS, GATE_ROWS) != (LSH_BANDS, LSH_ROWS)
    # Equal rows-per-band is the subtle trap: bands are consecutive permutation groups, so
    # (32,4) against the gate's (17,4) would make the gate's bands a strict PREFIX of
    # dedup's. Any pair the gate blocked, dedup would already have blocked, and the
    # tautology this gate exists to avoid returns through the back door.
    assert GATE_ROWS != LSH_ROWS, "equal rows-per-band makes the gate's bands a subset of dedup's"


def test_both_stages_reach_99_percent_blocking_recall_at_the_shared_threshold():
    assert gate_recall(GATE_JACCARD) >= 0.99
    assert lsh_recall(DEDUP_JACCARD) >= 0.98


def test_clean_bundle_passes_and_reports_max_cross_jaccard():
    report = assert_no_leakage(_bundle())
    assert report.clean
    assert report.method == "exact-all-pairs"
    assert 0.0 <= report.max_cross_jaccard < GATE_JACCARD


def test_injected_id_overlap_is_caught():
    bundle = _bundle()
    leaked = _replace_test(bundle, bundle.train_df.iloc[:1].copy())
    with pytest.raises(AssertionError, match="overlap"):
        assert_no_leakage(leaked)


def test_injected_exact_text_leak_is_caught():
    bundle = _bundle()
    row = bundle.test_df.iloc[0].copy()
    row["id"] = "leak_exact"
    row["comment_text"] = bundle.train_df.iloc[0]["comment_text"]
    leaked = _replace_test(bundle, pd.concat([bundle.test_df, row.to_frame().T], ignore_index=True))
    with pytest.raises(AssertionError, match="normalized text leak"):
        assert_no_leakage(leaked)


def test_gate_catches_a_duplicate_that_dedups_blocking_missed():
    """The gate's whole remaining job. dedup blocks at ~0.99 recall, not 1.0, so about one
    true duplicate pair in a hundred is never even compared to the threshold. The gate
    blocks with a different banding, so it still sees that residue.

    The miss is made deterministic here by crippling dedup's blocking to two bands of 64
    rows, whose recall at any realistic Jaccard is ~6e-5. (datasketch rejects b < 2, so a
    single band is not available.)
    """
    bundle = _bundle()
    source = bundle.train_df.iloc[0]["comment_text"]
    twin = f"{source} ."
    score = jaccard(shingle_set(normalize(source)), shingle_set(normalize(twin)))
    assert score >= DEDUP_JACCARD, "the probe must be a genuine duplicate by the shared rule"
    assert normalize(twin) != normalize(source), "must survive the exact-normalized pass"

    pair = pd.DataFrame(
        [bundle.train_df.iloc[0].to_dict(), {**bundle.train_df.iloc[0].to_dict(),
                                             "id": "zzz_twin", "comment_text": twin}]
    )
    assert len(dedup(pair, bands=2, rows=64)) == 2, "crippled blocking must miss the pair"
    assert len(dedup(pair)) == 1, "real blocking must catch it"

    row = bundle.test_df.iloc[0].copy()
    row["id"] = "leak_missed"
    row["comment_text"] = twin
    leaked = _replace_test(bundle, pd.concat([bundle.test_df, row.to_frame().T], ignore_index=True))
    with pytest.raises(AssertionError, match="near-duplicate leak"):
        assert_no_leakage(leaked)


def test_both_gate_paths_agree_on_the_same_bundle():
    bundle = _bundle()
    exact = leakage_report(bundle)
    blocked = leakage_report(bundle, exact_pair_budget=0)
    assert exact.method == "exact-all-pairs"
    assert blocked.method == "lsh-blocked-exact"
    assert exact.near_duplicate_pairs == blocked.near_duplicate_pairs == 0


def test_gate_reuses_cached_signatures_and_computes_none_of_its_own():
    clear_cache()
    bundle = prepare_dataset(FIXTURE, SplitConfig(seed=42))
    after_prepare = cache_stats()
    leakage_report(bundle, exact_pair_budget=0)
    after_gate = cache_stats()
    assert after_gate.misses == after_prepare.misses
    assert after_gate.hits >= len(bundle.train_df) + len(bundle.test_df)
