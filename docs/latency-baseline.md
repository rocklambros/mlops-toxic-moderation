# Latency baseline

Measured by `make loadtest` on 2026-08-11. Rewritten only when
`UPDATE_LATENCY_BASELINE` is set, so a routine `-m integration` run reports against these
numbers instead of replacing them.

| Measure | p50 | p95 | p99 |
|---|---|---|---|
| stamped `latency_ms` | 18.0 ms | 21.0 ms | 22.0 ms |
| client round trip | - | 29.5 ms | - |

Budget: p95 under 500 ms. Result: PASS.
Regression gate: a later run fails above 31.5 ms.

## What these numbers are, and are not

- **n = 200**, one run, no repetitions. There is no standard error
  here, so two runs differing by a millisecond or two are indistinguishable and
  a table diff of that size is not a measured change.
- **The percentiles are quantized.** `latency_ms` is an integer column, so p95
  interpolates between order statistics 190 and 191 of 200, and p99 between 198
  and 199 - p99 is the second and third largest observations of the run.
- **200 of 200 samples were flagged**, and only a flagged
  comment pays the review insert. Where that ratio is 1, these numbers describe
  the two-insert path exclusively and say nothing about the cheaper one; where
  it is between, the percentile is over a mixture whose composition is set by
  cycling a fixed 7-string corpus rather than by real traffic. The
  fixture model is deliberately tiny and is not calibrated, so a ratio of 1 here
  is a property of the fixture, not a prediction about production.
- **`predictions` held 201 rows** when this ran. Both inserts are inside the
  stamped interval, so index depth is part of the measurement.
- **`RANDOM_AUDIT_RATE` was 0.0**, which
  is not the deployed value; the audit insert does not fire here and does in
  production.

## Which artifact this measured

Model under test: `toxic-clf:v3`, loaded from the
deterministic fixture artifact built by `tests/fixtures/make_model.py`. That is not
the promoted production artifact: `MODEL_CARD.md` still carries the fail-closed
sentinel digest, so no promoted model exists to load. The fixture is two orders of
magnitude smaller than the production TF-IDF vocabulary, so these numbers bound the
**framework** cost - routing, validation, policy, both inserts - and not the
**inference** cost. Re-run `make loadtest` against the promoted artifact once
Phase 1 registers one, and record the result here before the graded demo.
