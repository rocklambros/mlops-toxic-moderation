"""Executable leakage-firewall gate. Independent of dedup by construction.

The previous gate imported dedup's `_minhash` and re-ran MinHashLSH at dedup's own
parameters. datasketch seeds MinHash deterministically, so the band hashes were
byte-identical and LSH banding is symmetric: a pair dedup failed to bucket could not be
bucketed by the gate. All three assertions passed by construction. The gate had zero
detection power.

Independence here is structural, not cosmetic:

  * The DECISION is exact shingle-set Jaccard, never a band collision.
  * The threshold is the SAME as dedup's. It used to be lower, which was unsatisfiable on
    a corpus with a continuum of similarities: the band between the two was never empty,
    so the gate could never pass. Independence comes from the banding instead -- dedup
    blocks at (25, 5), the gate at (17, 4), and rows-per-band differs so neither's bands
    are a subset of the other's.
  * Small inputs skip LSH entirely and compare all cross-split pairs exactly, so the unit
    tests exercise an algorithm with no machinery in common with dedup at all.
  * When LSH is used for blocking on the real corpus, it is banded at (17, 4), whose
    recall at J=0.70 is 1-(1-0.70**4)**17 = 0.991 -- a different S-curve from dedup's.

max_cross_jaccard is exact on the all-pairs path. On the blocked path it is the maximum
over the candidate set, which is a lower bound on the true maximum -- tight where it
matters, because anything near the threshold is blocked in with probability >= 0.99.
"""

from dataclasses import dataclass

from datasketch import MinHashLSH

from model.data.prepare import DatasetBundle
from model.data.shingles import NUM_PERM, jaccard, shingle_set, signature
from model.normalize import normalize

GATE_JACCARD = 0.70
GATE_BANDS = 17
GATE_ROWS = 4
EXACT_PAIR_BUDGET = 2_000_000


@dataclass(frozen=True)
class LeakageReport:
    id_overlap: int
    exact_text_leak: int
    near_duplicate_pairs: int
    max_cross_jaccard: float
    worst_pair: tuple[str, str] | None
    method: str

    def summary(self) -> str:
        return (
            f"method={self.method} id_overlap={self.id_overlap} "
            f"exact_text_leak={self.exact_text_leak} "
            f"near_duplicate_pairs={self.near_duplicate_pairs} "
            f"max_cross_jaccard={self.max_cross_jaccard:.4f} worst_pair={self.worst_pair}"
        )

    @property
    def clean(self) -> bool:
        return (
            self.id_overlap == 0
            and self.exact_text_leak == 0
            and self.near_duplicate_pairs == 0
        )


def gate_recall(jaccard_at: float = GATE_JACCARD) -> float:
    return 1.0 - (1.0 - jaccard_at**GATE_ROWS) ** GATE_BANDS


def _normalized(df) -> list[tuple[str, str]]:
    return [
        (str(rid), normalize(text))
        for rid, text in zip(df["id"], df["comment_text"], strict=True)
    ]


def leakage_report(
    bundle: DatasetBundle,
    threshold: float = GATE_JACCARD,
    exact_pair_budget: int = EXACT_PAIR_BUDGET,
) -> LeakageReport:
    train = _normalized(bundle.train_df)
    test = _normalized(bundle.test_df)

    id_overlap = len({t[0] for t in train} & {t[0] for t in test})
    exact_leak = len({t[1] for t in train} & {t[1] for t in test})

    train_shingles = {rid: shingle_set(norm) for rid, norm in train}
    hits: list[tuple[float, str, str]] = []
    best = (0.0, None)

    if len(train) * len(test) <= exact_pair_budget:
        method = "exact-all-pairs"
        for test_id, test_norm in test:
            test_sh = shingle_set(test_norm)
            for train_id, train_sh in train_shingles.items():
                score = jaccard(test_sh, train_sh)
                if score > best[0]:
                    best = (score, (train_id, test_id))
                if score >= threshold:
                    hits.append((score, train_id, test_id))
    else:
        method = "lsh-blocked-exact"
        lsh = MinHashLSH(num_perm=NUM_PERM, params=(GATE_BANDS, GATE_ROWS))
        for train_id, train_norm in train:
            lsh.insert(train_id, signature(train_norm))
        for test_id, test_norm in test:
            test_sh = shingle_set(test_norm)
            for train_id in lsh.query(signature(test_norm)):
                score = jaccard(test_sh, train_shingles[train_id])
                if score > best[0]:
                    best = (score, (train_id, test_id))
                if score >= threshold:
                    hits.append((score, train_id, test_id))

    return LeakageReport(
        id_overlap=id_overlap,
        exact_text_leak=exact_leak,
        near_duplicate_pairs=len(hits),
        max_cross_jaccard=best[0],
        worst_pair=best[1],
        method=method,
    )


def assert_no_leakage(
    bundle: DatasetBundle,
    threshold: float = GATE_JACCARD,
    exact_pair_budget: int = EXACT_PAIR_BUDGET,
) -> LeakageReport:
    report = leakage_report(bundle, threshold, exact_pair_budget)
    if report.id_overlap:
        raise AssertionError(f"train/test id overlap: {report.id_overlap} ids")
    if report.exact_text_leak:
        raise AssertionError(f"normalized text leak across split: {report.exact_text_leak} rows")
    if report.near_duplicate_pairs:
        raise AssertionError(
            f"near-duplicate leak across split: {report.near_duplicate_pairs} pairs at "
            f"Jaccard >= {threshold}; worst {report.worst_pair} at "
            f"{report.max_cross_jaccard:.4f}"
        )
    return report
