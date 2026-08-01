import pytest

from backend.ratelimit import MAX_TRACKED_KEYS, RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_burst_is_allowed_then_the_bucket_is_empty():
    """REG-6.3b. A public /predict on a public repo with no rate limit is free denial-of-
    service capacity, and delivery spec section 13 names this control as one of three that
    make the public-registry decision defensible."""
    limiter = RateLimiter(per_minute=60, burst=5, clock=FakeClock())
    assert [limiter.allow("k") for _ in range(5)] == [True] * 5
    assert limiter.allow("k") is False


def test_tokens_refill_at_the_configured_rate():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=1, clock=clock)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    clock.advance(1.0)  # 60/minute == one token per second
    assert limiter.allow("k") is True


def test_refill_is_capped_at_the_burst():
    """The bucket must be drawn down BEFORE the clock jumps, or the assertion proves nothing.

    A bucket is created holding a full burst, so advancing the clock before the first call
    leaves `min(burst, tokens + elapsed * rate)` measuring `min(burst, burst + 0)`: the cap
    could be deleted outright and the sequence would be unchanged. Spending one token first
    makes the cap load-bearing -- uncapped, 3600 seconds of refill grants 3600 tokens and the
    fourth call succeeds.
    """
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, burst=3, clock=clock)
    assert limiter.allow("k") is True
    clock.advance(3600.0)
    assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]


def test_keys_are_isolated():
    limiter = RateLimiter(per_minute=60, burst=1, clock=FakeClock())
    assert limiter.allow("first") is True
    assert limiter.allow("first") is False
    assert limiter.allow("second") is True


def test_the_key_table_is_bounded():
    """The limiter is itself a memory-growth primitive if it tracks unbounded keys."""
    limiter = RateLimiter(per_minute=60, burst=1, clock=FakeClock())
    for index in range(MAX_TRACKED_KEYS + 500):
        limiter.allow(f"key-{index}")
    assert len(limiter._buckets) <= MAX_TRACKED_KEYS


@pytest.mark.parametrize("kwargs", [{"per_minute": 0, "burst": 1}, {"per_minute": 5, "burst": 0}])
def test_nonsense_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(**kwargs)
