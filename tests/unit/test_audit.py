import random

import pytest

from backend.audit import FLAGGED_INCLUSION_PROBABILITY, should_random_audit


def test_flagged_rows_are_sampled_with_certainty():
    assert FLAGGED_INCLUSION_PROBABILITY == 1.0


def test_rate_zero_never_audits():
    rng = random.Random(0)
    assert not any(should_random_audit(0.0, rng) for _ in range(500))


def test_rate_one_always_audits():
    rng = random.Random(0)
    assert all(should_random_audit(1.0, rng) for _ in range(500))


def test_sampling_is_deterministic_for_a_seeded_generator():
    first = [should_random_audit(0.05, random.Random(7)) for _ in range(1)]
    second = [should_random_audit(0.05, random.Random(7)) for _ in range(1)]
    assert first == second


def test_observed_rate_tracks_the_requested_rate():
    rng = random.Random(1234)
    hits = sum(should_random_audit(0.05, rng) for _ in range(20000))
    assert 0.04 < hits / 20000 < 0.06


@pytest.mark.parametrize("rate", [-0.01, 1.01])
def test_rate_outside_zero_to_one_is_rejected(rate):
    with pytest.raises(ValueError, match="between 0 and 1"):
        should_random_audit(rate, random.Random(0))
