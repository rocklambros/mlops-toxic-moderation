from pathlib import Path

from datasketch import MinHashLSH

from model.data.dedup import (
    DEDUP_JACCARD,
    LSH_BANDS,
    LSH_ROWS,
    dedup,
    lsh_recall,
)
from model.data.load import load_raw
from model.data.shingles import NUM_PERM, jaccard, shingle_set
from model.normalize import normalize

FIXTURE = Path("tests/fixtures/mini_jigsaw.csv")


def test_lsh_banding_reaches_98_percent_recall_at_the_operating_threshold():
    """(25, 5) gives 0.9899 at J=0.70, which is the best available at rows=5 inside 128
    permutations. rows must differ from the gate's 4, or the gate's bands become a strict
    prefix of dedup's and it can never catch a dedup miss -- so (32, 4), which scores
    0.9998, is disqualified. (26, 5) would need 130 permutations.

    The residual ~1% miss sits exactly AT the threshold and decays fast above it: recall is
    0.9989 by J=0.75. Boundary pairs are the least costly to miss, and the gate blocks with
    a different banding specifically to catch that residue.
    """
    recall = lsh_recall(DEDUP_JACCARD)
    assert recall >= 0.98, (
        f"blocking recall at J={DEDUP_JACCARD} is {recall:.4f} with "
        f"b={LSH_BANDS}, r={LSH_ROWS}"
    )
    assert lsh_recall(0.75) >= 0.99, "recall must recover quickly above the threshold"
    assert LSH_BANDS * LSH_ROWS <= NUM_PERM


def test_datasketch_threshold_auto_tuning_would_not_reach_that_bar():
    """Stated relatively rather than pinned to a banding, because the auto-tuner's output
    is a function of the threshold and the previous version hardcoded (9, 13) from J=0.80.
    """
    auto = MinHashLSH(threshold=DEDUP_JACCARD, num_perm=NUM_PERM)
    auto_recall = 1 - (1 - DEDUP_JACCARD**auto.r) ** auto.b
    assert auto_recall < 0.5, f"auto-tuned recall {auto_recall:.4f} at J={DEDUP_JACCARD}"
    assert auto_recall < lsh_recall(DEDUP_JACCARD) / 2
    assert (auto.b, auto.r) != (LSH_BANDS, LSH_ROWS)


def test_configured_lsh_uses_the_explicit_banding():
    lsh = MinHashLSH(num_perm=NUM_PERM, params=(LSH_BANDS, LSH_ROWS))
    assert (lsh.b, lsh.r) == (LSH_BANDS, LSH_ROWS)
    assert lsh.b * lsh.r <= NUM_PERM


def test_dedup_collapses_exact_and_near_duplicates_and_reconciles_labels():
    df = load_raw(FIXTURE)
    out = dedup(df)
    assert len(out) == len(df) - 4
    norm = [normalize(t) for t in out["comment_text"]]
    assert norm.count("you are an idiot") == 1
    assert "you are an idiot!" not in norm
    merged = out[out["comment_text"].map(normalize) == "i will kill you"].iloc[0]
    assert merged["insult"] == 1 and merged["threat"] == 1


def test_planted_near_duplicate_is_above_the_exact_verification_threshold():
    a = shingle_set(normalize("you are an idiot"))
    b = shingle_set(normalize("you are an idiot!"))
    assert jaccard(a, b) >= DEDUP_JACCARD


def test_dedup_keeps_distinct_low_similarity_rows():
    out = dedup(load_raw(FIXTURE))
    assert any(normalize(t) == "have a nice day friend" for t in out["comment_text"])
    assert any(normalize(t) == "i am going to hurt you" for t in out["comment_text"])


def test_dedup_never_collapses_a_pair_below_the_exact_threshold():
    out = dedup(load_raw(FIXTURE))
    texts = [normalize(t) for t in out["comment_text"]]
    shingles = [shingle_set(t) for t in texts]
    worst = max(
        jaccard(shingles[i], shingles[j])
        for i in range(len(shingles))
        for j in range(i + 1, len(shingles))
    )
    assert worst < DEDUP_JACCARD


def test_dedup_is_idempotent():
    df = load_raw(FIXTURE)
    once = dedup(df)
    twice = dedup(once)
    assert list(once["id"]) == list(twice["id"])
    assert once.equals(twice)


def test_representative_is_the_minimum_id_not_query_order():
    df = load_raw(FIXTURE)
    out = dedup(df)
    kept = out[out["comment_text"].map(normalize) == "you are an idiot"]
    assert list(kept["id"]) == ["c016"]
    shuffled = df.sample(frac=1.0, random_state=1).reset_index(drop=True)
    assert list(dedup(shuffled)["id"]) == list(out["id"])
