"""One load pass against a real Postgres, so the graded latency chart has a stated budget
rather than an anecdote.

Premortem H28: the design had no latency budget, no percentile, and no load test, while
`latency_ms` is a graded artifact (rubric 3.2, latency over time). This measures what the
system actually stamps - the whole handler including persistence - and fails if p95 exceeds
the budget in `Settings.latency_budget_p95_ms`.
"""

import statistics
import time
from pathlib import Path

import pytest

from backend.ratelimit import RateLimiter
from tests.integration.conftest import AUTH

pytestmark = [pytest.mark.perf, pytest.mark.integration]

SAMPLES = 200
# Anchored to the repository, not to the working directory: the baseline is a committed
# artifact, and a cwd-relative path silently writes a stray docs/ tree wherever pytest
# happened to be invoked from.
BASELINE = Path(__file__).resolve().parents[2] / "docs" / "latency-baseline.md"

CORPUS = [
    "have a nice day friend",
    "i disagree but respect your point",
    "you are an idiot",
    "what the hell is this crap",
    "watch your back i am coming",
    "your kind does not belong here",
    "thanks for the thoughtful edit " * 40,
]


def test_p95_latency_under_budget(client):
    client.app.state.limiter = RateLimiter(per_minute=600_000, burst=SAMPLES + 10)

    client.post("/predict", json={"text": "warm up"}, headers=AUTH)      # JIT + pool warm

    stamped: list[int] = []
    observed: list[float] = []
    for index in range(SAMPLES):
        text = CORPUS[index % len(CORPUS)]
        started = time.perf_counter()
        response = client.post("/predict", json={"text": text}, headers=AUTH)
        observed.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
        stamped.append(response.json()["latency_ms"])

    percentiles = statistics.quantiles(stamped, n=100)
    p50, p95, p99 = statistics.median(stamped), percentiles[94], percentiles[98]
    round_trip_p95 = statistics.quantiles(observed, n=100)[94]

    budget = client.app.state.settings.latency_budget_p95_ms
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(
        "# Latency baseline\n\n"
        f"Measured by `make loadtest`: {SAMPLES} sequential single-comment requests against a\n"
        "real Postgres, warm process, no concurrency. `latency_ms` is the value the service\n"
        "stamps and stores, measured from handler entry through the prediction and review\n"
        "inserts; the client round trip additionally includes serialization and the commit.\n\n"
        "| Measure | p50 | p95 | p99 |\n|---|---|---|---|\n"
        f"| stamped `latency_ms` | {p50:.0f} ms | {p95:.0f} ms | {p99:.0f} ms |\n"
        f"| client round trip | - | {round_trip_p95:.0f} ms | - |\n\n"
        f"Budget: p95 under {budget} ms. "
        f"Result: {'PASS' if p95 < budget else 'FAIL'}.\n\n"
        "## Which artifact this measured\n\n"
        f"Model under test: `{client.app.state.model.public_version}`, loaded from the\n"
        "deterministic fixture artifact built by `tests/fixtures/make_model.py`. That is not\n"
        "the promoted production artifact: `MODEL_CARD.md` still carries the fail-closed\n"
        "sentinel digest, so no promoted model exists to load. The fixture is two orders of\n"
        "magnitude smaller than the production TF-IDF vocabulary, so these numbers bound the\n"
        "**framework** cost - routing, validation, policy, both inserts - and not the\n"
        "**inference** cost. Re-run `make loadtest` against the promoted artifact once Phase 1\n"
        "registers one, and record the result here before the graded demo.\n",
        encoding="utf-8",
    )

    print(f"\nlatency p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms")
    assert p95 < client.app.state.settings.latency_budget_p95_ms


def test_the_stamped_latency_is_not_systematically_smaller_than_the_round_trip(client):
    """A stamped value far below the observed round trip would mean the stamp is being taken
    before the work it is supposed to cover - the H28 failure, in numbers."""
    client.app.state.limiter = RateLimiter(per_minute=600_000, burst=100)
    stamped, observed = [], []
    for _ in range(30):
        started = time.perf_counter()
        response = client.post("/predict", json={"text": "you are an idiot"}, headers=AUTH)
        observed.append((time.perf_counter() - started) * 1000)
        stamped.append(response.json()["latency_ms"])
    assert statistics.median(stamped) >= 0.5 * statistics.median(observed)
