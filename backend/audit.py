"""Random-audit sampling for the review queue.

Live accuracy is the graded metric (rubric 3.2), and computing it over the model's own
flagged set is structurally blind to confidently-allowed false negatives - the costly missed
`threat`. A random-audit stratum fixes that only if the two strata are weighted, which
requires each row to carry the probability with which it was selected (premortem H8). The
sampler therefore returns a decision, and the caller writes the corresponding inclusion
probability onto the review row.

The generator is injected. Production uses `random.SystemRandom()`: with a public repository
and a seeded PRNG, an attacker could compute which requests will be audited and time
submissions to miss the sample.
"""

import random

FLAGGED_INCLUSION_PROBABILITY: float = 1.0


def should_random_audit(rate: float, rng: random.Random) -> bool:
    if not 0.0 <= rate <= 1.0:
        raise ValueError(f"random audit rate must be between 0 and 1 inclusive, got {rate}")
    return rng.random() < rate
