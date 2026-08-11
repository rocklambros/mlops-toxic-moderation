"""One load pass against a real Postgres, so the graded latency chart has a stated budget
rather than an anecdote.

Premortem H28: the design had no latency budget, no percentile, and no load test, while
`latency_ms` is a graded artifact (rubric 3.2, latency over time). This measures what the
system actually stamps - the whole handler including persistence - and fails if p95 exceeds
the budget in `Settings.latency_budget_p95_ms`.
"""

import datetime as dt
import os
import re
import statistics
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from backend.ratelimit import RateLimiter
from tests.integration.conftest import AUTH

pytestmark = [pytest.mark.perf, pytest.mark.integration]

SAMPLES = 200
# Anchored to the repository, not to the working directory: the baseline is a committed
# artifact, and a cwd-relative path silently writes a stray docs/ tree wherever pytest
# happened to be invoked from.
BASELINE = Path(__file__).resolve().parents[2] / "docs" / "latency-baseline.md"

# The file is only rewritten when a run says it means to. Until 2026-08-11 the write was
# unconditional, and `pytestmark` above puts this test in `-m integration` -- so `make
# test-integration`, `make test-cov` and the `test` job in CI all rewrote a committed
# artifact as a side effect. The committed numbers were therefore whichever run last
# happened to finish on somebody's machine, and the document's own claim to have been
# "Measured by `make loadtest`" was false for most of the runs that produced it.
#
# `make loadtest` sets this; nothing else does.
UPDATE_ENV = "UPDATE_LATENCY_BASELINE"

# The regression gate, which is the thing a baseline is FOR and which this file did not have.
# The only assertion used to be `p95 < 500`, so p95 could go 18 ms -> 499 ms -- a 27x
# regression -- and the run would pass, rewrite the document with the new number, and stamp
# it `Result: PASS`. The measurement of record and the verdict were both produced by the run
# being judged.
#
# Tolerance is multiplicative with an absolute floor because the quantity is small, integer
# valued and unreplicated: p95 is one order statistic of 200 integer samples, so neighbouring
# runs legitimately differ by a millisecond or two and a tight relative bound would flap. The
# floor is what keeps 18 -> 20 quiet; the multiplier is what makes 18 -> 499 loud.
REGRESSION_MULTIPLIER = 1.5
REGRESSION_FLOOR_MS = 10.0


def _committed_p95() -> float | None:
    """The p95 recorded in the committed document, or None if there is not one to compare to.

    Parsed rather than imported because the document is the artifact under version control:
    a regression is a disagreement between what is committed and what this run measures, and
    reading anything else would compare the run to itself.
    """
    if not BASELINE.exists():
        return None
    row = re.search(r"\|\s*stamped[^|]*\|[^|]*\|\s*([0-9.]+)\s*ms\s*\|", BASELINE.read_text())
    return float(row.group(1)) if row else None

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
    flagged = 0
    for index in range(SAMPLES):
        comment = CORPUS[index % len(CORPUS)]
        started = time.perf_counter()
        response = client.post("/predict", json={"text": comment}, headers=AUTH)
        observed.append((time.perf_counter() - started) * 1000)
        assert response.status_code == 200
        body = response.json()
        stamped.append(body["latency_ms"])
        # Counted, because it is the composition of the sample. Only a flagged comment pays
        # the second insert, so a corpus whose flag rate moves changes the measured
        # percentile without anything in the service having changed.
        flagged += any(label["flag"] for label in body["labels"].values())

    percentiles = statistics.quantiles(stamped, n=100)
    p50, p95, p99 = statistics.median(stamped), percentiles[94], percentiles[98]
    round_trip_p95 = statistics.quantiles(observed, n=100)[94]

    budget = client.app.state.settings.latency_budget_p95_ms
    previous = _committed_p95()

    if os.environ.get(UPDATE_ENV):
        with client.app.state.engine.connect() as conn:
            rows = conn.execute(text("SELECT count(*) FROM predictions")).scalar()
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(
            "# Latency baseline\n\n"
            f"Measured by `make loadtest` on {dt.date.today().isoformat()}. Rewritten only when\n"
            f"`{UPDATE_ENV}` is set, so a routine `-m integration` run reports against these\n"
            "numbers instead of replacing them.\n\n"
            "| Measure | p50 | p95 | p99 |\n|---|---|---|---|\n"
            f"| stamped `latency_ms` | {p50:.1f} ms | {p95:.1f} ms | {p99:.1f} ms |\n"
            f"| client round trip | - | {round_trip_p95:.1f} ms | - |\n\n"
            f"Budget: p95 under {budget} ms. Result: {'PASS' if p95 < budget else 'FAIL'}.\n"
            f"Regression gate: a later run fails above "
            f"{max(p95 * REGRESSION_MULTIPLIER, p95 + REGRESSION_FLOOR_MS):.1f} ms.\n\n"
            "## What these numbers are, and are not\n\n"
            f"- **n = {len(stamped)}**, one run, no repetitions. There is no standard error\n"
            "  here, so two runs differing by a millisecond or two are indistinguishable and\n"
            "  a table diff of that size is not a measured change.\n"
            f"- **The percentiles are quantized.** `latency_ms` is an integer column, so p95\n"
            "  interpolates between order statistics 190 and 191 of 200, and p99 between 198\n"
            "  and 199 - p99 is the second and third largest observations of the run.\n"
            f"- **{flagged} of {len(stamped)} samples were flagged**, and only a flagged\n"
            "  comment pays the review insert. Where that ratio is 1, these numbers describe\n"
            "  the two-insert path exclusively and say nothing about the cheaper one; where\n"
            "  it is between, the percentile is over a mixture whose composition is set by\n"
            f"  cycling a fixed {len(CORPUS)}-string corpus rather than by real traffic. The\n"
            "  fixture model is deliberately tiny and is not calibrated, so a ratio of 1 here\n"
            "  is a property of the fixture, not a prediction about production.\n"
            f"- **`predictions` held {rows} rows** when this ran. Both inserts are inside the\n"
            "  stamped interval, so index depth is part of the measurement.\n"
            f"- **`RANDOM_AUDIT_RATE` was {client.app.state.settings.random_audit_rate}**, which\n"
            "  is not the deployed value; the audit insert does not fire here and does in\n"
            "  production.\n\n"
            "## Which artifact this measured\n\n"
            f"Model under test: `{client.app.state.model.public_version}`, loaded from the\n"
            "deterministic fixture artifact built by `tests/fixtures/make_model.py`. That is not\n"
            "the promoted production artifact: `MODEL_CARD.md` still carries the fail-closed\n"
            "sentinel digest, so no promoted model exists to load. The fixture is two orders of\n"
            "magnitude smaller than the production TF-IDF vocabulary, so these numbers bound the\n"
            "**framework** cost - routing, validation, policy, both inserts - and not the\n"
            "**inference** cost. Re-run `make loadtest` against the promoted artifact once\n"
            "Phase 1 registers one, and record the result here before the graded demo.\n",
            encoding="utf-8",
        )

    print(f"\nlatency p50={p50:.1f}ms p95={p95:.1f}ms p99={p99:.1f}ms (committed {previous})")
    assert p95 < budget, f"p95 {p95:.1f} ms exceeds the {budget} ms budget"

    if previous is not None:
        ceiling = max(previous * REGRESSION_MULTIPLIER, previous + REGRESSION_FLOOR_MS)
        assert p95 <= ceiling, (
            f"p95 regressed: {p95:.1f} ms against a committed baseline of {previous:.1f} ms "
            f"(ceiling {ceiling:.1f} ms). Re-run with {UPDATE_ENV}=1 to accept the new number "
            f"if the change is intended."
        )


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
