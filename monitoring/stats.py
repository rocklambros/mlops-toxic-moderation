"""Estimation primitives for the monitoring dashboard.

Live accuracy is collected from two strata with different inclusion probabilities: every
flagged item is reviewed (pi = 1.0) and a fraction of the rest is audited (pi =
RANDOM_AUDIT_RATE). Reporting correct/total over the union is biased toward whichever
stratum happens to be larger. The Horvitz-Thompson estimator weights each observation by
1/pi, which is unbiased for the population mean under this design.

The interval is a Wilson score interval evaluated at Kish's effective sample size,
n_eff = (sum w)^2 / sum(w^2). Two separate mistakes are avoided by that choice. Evaluating
at the raw n would claim 220 unequally-weighted observations carry the information of 220
independent ones. Using the normal approximation instead of Wilson would put the upper
bound above 1.0 wherever the counts are small and the proportion sits near 1 -- which is
exactly where a review queue lives -- and print an impossible number on a graded screenshot.

A stratum is a design cell, not a label: two rows both marked `random-audit` but drawn at
different rates were drawn under different designs, because RANDOM_AUDIT_RATE is deploy
configuration and configuration changes. They are therefore reported as two rows rather
than folded together under one of the two rates.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

Z95 = 1.959963984540054


def wilson_interval(successes: float, n: float, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    p = min(max(p, 0.0), 1.0)
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class StratumStat:
    stratum: str
    n: int
    correct: int
    sample_rate: float
    accuracy: float | None
    lo: float | None
    hi: float | None


@dataclass(frozen=True)
class AccuracyReport:
    n: int
    point: float | None
    lo: float | None
    hi: float | None
    effective_n: float
    strata: list[StratumStat]


def horvitz_thompson_accuracy(
    rows: Iterable[tuple[str, float, bool]],
) -> AccuracyReport:
    materialised = list(rows)
    if not materialised:
        return AccuracyReport(n=0, point=None, lo=None, hi=None, effective_n=0.0, strata=[])

    numerator = 0.0
    denominator = 0.0
    sum_w = 0.0
    sum_w2 = 0.0
    buckets: dict[tuple[str, float], list[int]] = {}

    for stratum, sample_rate, correct in materialised:
        if sample_rate is None or sample_rate <= 0.0 or sample_rate > 1.0:
            raise ValueError(
                f"sample_rate must be in (0, 1] for stratum {stratum!r}; got {sample_rate!r}. "
                "A reviewed row without a recorded inclusion probability cannot be weighted."
            )
        weight = 1.0 / sample_rate
        numerator += weight * (1.0 if correct else 0.0)
        denominator += weight
        sum_w += weight
        sum_w2 += weight * weight
        bucket = buckets.setdefault((stratum, float(sample_rate)), [0, 0])
        bucket[0] += 1
        bucket[1] += 1 if correct else 0

    point = numerator / denominator
    effective_n = (sum_w * sum_w) / sum_w2
    lo, hi = wilson_interval(point * effective_n, effective_n)

    strata = []
    for name, rate in sorted(buckets):
        n, correct = buckets[(name, rate)]
        s_lo, s_hi = wilson_interval(correct, n)
        strata.append(
            StratumStat(
                stratum=name,
                n=n,
                correct=correct,
                sample_rate=rate,
                accuracy=correct / n if n else None,
                lo=s_lo,
                hi=s_hi,
            )
        )

    return AccuracyReport(
        n=len(materialised),
        point=point,
        lo=lo,
        hi=hi,
        effective_n=effective_n,
        strata=strata,
    )


def psi(p_ref: float, p_prod: float, eps: float = 1e-6) -> float:
    """Population Stability Index over the two-bin distribution [p, 1-p].

    Bands are the industry-standard reading: < 0.1 no meaningful shift, 0.1-0.2 moderate,
    >= 0.2 major. `eps` floors each bin so an all-zero reference cannot produce log(0).
    """
    total = 0.0
    for ref, prod in ((p_ref, p_prod), (1.0 - p_ref, 1.0 - p_prod)):
        ref = max(ref, eps)
        prod = max(prod, eps)
        total += (prod - ref) * math.log(prod / ref)
    return total


def js_divergence(p_ref: float, p_prod: float, eps: float = 1e-12) -> float:
    """Jensen-Shannon divergence in bits over [p, 1-p]. Bounded in [0, 1]."""

    def kl(p: list[float], q: list[float]) -> float:
        total = 0.0
        for a, b in zip(p, q, strict=True):
            a = max(a, eps)
            b = max(b, eps)
            total += a * math.log2(a / b)
        return total

    ref = [p_ref, 1.0 - p_ref]
    prod = [p_prod, 1.0 - p_prod]
    mid = [(a + b) / 2.0 for a, b in zip(ref, prod, strict=True)]
    return 0.5 * kl(ref, mid) + 0.5 * kl(prod, mid)
