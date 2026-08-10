# Latency baseline

Measured by `make loadtest`: 200 sequential single-comment requests against a
real Postgres, warm process, no concurrency. `latency_ms` is the value the service
stamps and stores, measured from handler entry through the prediction and review
inserts; the client round trip additionally includes serialization and the commit.

| Measure | p50 | p95 | p99 |
|---|---|---|---|
| stamped `latency_ms` | 16 ms | 19 ms | 20 ms |
| client round trip | - | 27 ms | - |

Budget: p95 under 500 ms. Result: PASS.

## Which artifact this measured

Model under test: `toxic-clf:v3`, loaded from the
deterministic fixture artifact built by `tests/fixtures/make_model.py`. That is not
the promoted production artifact: `MODEL_CARD.md` still carries the fail-closed
sentinel digest, so no promoted model exists to load. The fixture is two orders of
magnitude smaller than the production TF-IDF vocabulary, so these numbers bound the
**framework** cost - routing, validation, policy, both inserts - and not the
**inference** cost. Re-run `make loadtest` against the promoted artifact once Phase 1
registers one, and record the result here before the graded demo.
