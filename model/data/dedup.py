"""Deterministic near-duplicate dedup. Runs before any split (leakage firewall).

Two stages, and only the second one is probabilistic:

1. LSH BLOCKING. MinHash LSH nominates candidate pairs. Banding is passed explicitly as
   params=(16, 6), NOT via `threshold=`. datasketch's auto-tuner minimises a balanced
   false-positive/false-negative error INTEGRAL, not recall at the decision point:
   MinHashLSH(threshold=0.70, num_perm=128) auto-tunes to a banding whose recall at the
   operating point is far below 1. params=(25, 5) gives 1-(1-0.70**5)**25 = 0.990.

   The threshold moved 0.80 -> 0.70 on 2026-07-31 after measuring the real corpus: 8,711
   cross-split pairs sat in [0.70, 0.80), touching 564 distinct test rows, 1.75% of the
   held-out set. Collapsing them costs 2,160 more rows, 1.01% of the corpus. Trading 1%
   of training data for an uncontaminated measurement instrument is the right direction:
   there are 214,744 training rows and exactly one held-out set.

   The banding had to move with it. (16, 6) has only 0.865 recall at J=0.70, so the
   threshold change alone would have made dedup miss 13.5% of true duplicates. (32, 4)
   scores better still but is DISQUALIFIED: bands are consecutive permutation groups, so
   the gate's (17, 4) bands would be a strict prefix of dedup's and the gate could never
   catch a dedup miss. Rows-per-band must differ from the gate's.
2. EXACT VERIFICATION. Every candidate is confirmed with exact char-shingle Jaccard
   against DEDUP_JACCARD before anything collapses. LSH decides nothing.

Labels are reconciled by OR across every collapsed group so a rare-label positive (a
`threat` under 0.3% of the corpus) is never discarded with a duplicate copy. The surviving
representative is `min(...)` over the verified candidate ids, never `hits[0]`:
MinHashLSH.query returns list(set(...)), whose order varies with PYTHONHASHSEED.
"""

import pandas as pd
from datasketch import MinHashLSH

from model.data.shingles import NUM_PERM, SHINGLE_K, jaccard, shingle_set, signature
from model.labels import LABELS
from model.normalize import normalize

DEDUP_JACCARD = 0.70
LSH_BANDS = 25
LSH_ROWS = 5


def lsh_recall(jaccard_at: float, bands: int = LSH_BANDS, rows: int = LSH_ROWS) -> float:
    """P(at least one band collides) for a pair at the given true Jaccard."""
    return 1.0 - (1.0 - jaccard_at**rows) ** bands


def _collapse_exact(df: pd.DataFrame) -> pd.DataFrame:
    """Exact-normalized collapse. Keeps the lowest id and ORs the six labels."""
    agg = {"id": "first", "comment_text": "first"}
    agg.update({label: "max" for label in LABELS})
    out = df.sort_values("id").groupby("_norm", sort=False, as_index=False).agg(agg)
    for label in LABELS:
        out[label] = out[label].astype(int)
    return out.sort_values("id").reset_index(drop=True)


def dedup(
    df: pd.DataFrame,
    jaccard_threshold: float = DEDUP_JACCARD,
    num_perm: int = NUM_PERM,
    bands: int = LSH_BANDS,
    rows: int = LSH_ROWS,
) -> pd.DataFrame:
    work = df.copy()
    work["_norm"] = work["comment_text"].map(normalize)
    exact = _collapse_exact(work)

    lsh = MinHashLSH(num_perm=num_perm, params=(bands, rows))
    kept: list[dict] = []
    row_at: dict[str, int] = {}
    shingles_at: dict[str, frozenset[str]] = {}

    for record in exact.to_dict("records"):
        rid = str(record["id"])
        norm = record["_norm"]
        sh = shingle_set(norm, SHINGLE_K)
        sig = signature(norm, num_perm)

        verified = [
            cid
            for cid in lsh.query(sig)
            if jaccard(sh, shingles_at[cid]) >= jaccard_threshold
        ]
        if verified:
            rep = row_at[min(verified)]
            for label in LABELS:
                kept[rep][label] = int(max(kept[rep][label], record[label]))
            continue

        lsh.insert(rid, sig)
        row_at[rid] = len(kept)
        shingles_at[rid] = sh
        kept.append(record)

    out = pd.DataFrame(kept, columns=["id", "comment_text", *LABELS, "_norm"])
    return out.drop(columns="_norm").reset_index(drop=True)
