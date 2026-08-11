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

    The bands say nothing about how many observations `p_prod` was computed from, and this
    function is not given the chance to ask: `psi(0.0961, 1/3)` is 0.35 whether that third
    came from one comment or four hundred. The floor that makes the difference is the
    caller's, `monitoring.queries.MIN_DRIFT_SAMPLES`.
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


def observation_is_improbable(baseline_rate: float, observed_rate: float, n: int,
                              alpha: float = 0.01) -> bool:
    """True when `n` draws at `baseline_rate` would rarely land this far from it.

    PSI answers "how far apart are these two rates" and nothing else. Its value does not move
    with the sample size: against a 0.0961 baseline an observed rate of zero scores 1.112
    whether it was measured over thirty predictions or ten thousand. So a PSI threshold alone
    cannot tell a real shift from a quiet afternoon, and a fixed floor on `n` cannot either --
    a floor calibrated for a 10 percent baseline is far too low for `threat` at 0.003, where
    seeing no flags in thirty predictions is the ordinary case rather than a signal.

    This supplies the missing half: the one-sided binomial tail probability of an observation
    at least this extreme, given the baseline. Thirty predictions with no flags against a
    0.0961 baseline is a one-in-twenty-one event, which is not evidence. Sixty is
    one-in-four-hundred, which is. The same arithmetic adapts itself per label, so the rare
    labels demand the larger sample they actually need.

    Implemented with an exact binomial tail rather than a normal approximation because the
    interesting cases sit at zero successes and small `n`, which is exactly where the normal
    approximation is worst.
    """
    if n <= 0:
        return False
    p = min(max(float(baseline_rate), 0.0), 1.0)
    if p <= 0.0 or p >= 1.0:
        return False
    successes = int(round(float(observed_rate) * n))
    successes = min(max(successes, 0), n)
    expected = p * n

    # Computed in log space. The direct form overflows: math.comb(2000, 600) does not fit in
    # a float, and the graded window routinely holds two thousand rows.
    log_p, log_q = math.log(p), math.log1p(-p)
    log_n_fact = math.lgamma(n + 1)

    def log_pmf(k: int) -> float:
        return (
            log_n_fact
            - math.lgamma(k + 1)
            - math.lgamma(n - k + 1)
            + k * log_p
            + (n - k) * log_q
        )

    # Sum the tail on the side the observation actually fell, so a rate above the baseline and
    # a rate below it are both answerable.
    ks = range(0, successes + 1) if successes <= expected else range(successes, n + 1)
    terms = [log_pmf(k) for k in ks]
    if not terms:
        return False
    # Subtract the largest term before exponentiating, so the sum stays inside float range
    # even when every individual probability underflows.
    peak = max(terms)
    tail = math.exp(peak) * sum(math.exp(term - peak) for term in terms)
    return tail < alpha
