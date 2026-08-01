# mlops-toxic-moderation

Production-grade Toxic Comment Moderation MLOps system. COMP 4450 final project.

A multi-label classifier (six toxicity labels: `toxic`, `severe_toxic`, `obscene`,
`threat`, `insult`, `identity_hate`) trained on the Jigsaw English dataset, deployed
end-to-end on AWS with experiment tracking, a model registry, a served API, a
human-review workflow, monitoring, and a CI/CD gate.

Design: [`docs/2026-07-01-toxic-moderation-mlops-design.md`](docs/2026-07-01-toxic-moderation-mlops-design.md).
Implementation plan: [`docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`](docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md).

## Endpoints

`GET /health` returns 200 with `{"status": "ok" | "degraded", "model_version", "database",
"spool_depth", "rejected"}`. The deploy gate asserts `status == "ok"` per instance; a
`degraded` status means the process is serving but the database or the spool needs attention.
It is unauthenticated, and it reports the opaque model version only — never the artifact
digest.

### Example request

```bash
curl -sS -X POST "http://$BACKEND_HOST:8000/predict" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $DEMO_API_KEY" \
  -d '{"text":"you are an idiot"}'
```

```json
{"request_id":"6f1c...","model_version":"toxic-clf:v3","labels":{"toxic":{"prob":0.87,"flag":true},
"severe_toxic":{"prob":0.12,"flag":false},"obscene":{"prob":0.44,"flag":false},
"threat":{"prob":0.03,"flag":false},"insult":{"prob":0.71,"flag":true},
"identity_hate":{"prob":0.05,"flag":false}},"decision":"review","max_prob":0.87,"latency_ms":42}
```

`DEMO_API_KEY` is not published here. It travels with the assignment submission and is rotated
after grading. Comments are capped at 4000 characters, request bodies at 16 KB, and each key is
limited to 30 requests per minute; requests without a valid key are rejected with 401 before
the body is parsed.

## Running the whole stack locally

```bash
docker compose -f infra/docker-compose.yml up -d --build      # five graded components
docker compose -f infra/docker-compose.yml --profile challenger up -d   # plus the re-scorer
```

Every credential is an interpolated variable with no default, so a missing one fails the
`up` rather than starting the reviewer console on a secret nobody chose. The DistilBERT
re-scorer sits behind the `challenger` profile because it is below the delivery plan's
cut-line: removing it is a profile that is not selected, with no Terraform edit, no instance
resize and no failing test.

| Surface | Port | Exposure |
|---|---|---|
| Backend API | 8000 | Demo ingress |
| User UI | 8501 | Demo ingress |
| Monitoring dashboard | 8502 | Demo ingress |
| Reviewer console | 8503 | **Operator only** — loopback locally, its own security group in AWS, reached over an SSM port forward |

### Demo dataset and monitoring

The monitoring dashboard is populated by `make seed-demo`, which replays roughly 2,000
comments from the **locked held-out split** through `/predict` and back-dates their
timestamps across 14 days. Predictions are real and their latency is measured; only the
timestamp is written by the operator tool. Every seeded row carries
`predictions.is_seed = true` and every seeded review carries `reviewer_id = 'seed-replay'`,
and the dashboard states how many of the displayed rows are seeded.

Two things the seeder needs from the operator, both deliberate:

- It replays through the real `/predict` route from one peer address, so the backend's rate
  limit has to be raised for the duration (`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST` on
  the backend service) and lowered again afterwards. The seeder does not retry around a 429:
  an abuse control that a tool quietly works around is not a control.
- It exits non-zero unless the resulting dataset would leave every graded panel non-
  degenerate, and one of those criteria is a non-empty random-audit stratum. The audit
  samples the traffic the model *allows*, so it stays empty until a model that allows some
  traffic is promoted. That refusal is the criterion working, not a bug in the seeder.

Live accuracy is a Horvitz-Thompson estimate over two probability-sampled strata: every
flagged item is reviewed (inclusion probability 1.0) and a `RANDOM_AUDIT_RATE` fraction of
the rest is audited. The inclusion probability is stored on each `review_queue` row at
enqueue time, so the estimate stays sound when the rate is changed. Per-stratum counts and a
95% Wilson interval are shown alongside the point estimate.

User feedback (the agree/disagree control on the user UI) is collected, stored with
`feedback.source = 'user'`, and displayed with its own n and interval. It is deliberately
**excluded** from the live-accuracy estimate, because a self-selected click has no known
inclusion probability and because a graded metric must not be writable by an anonymous
visitor. A disagreement instead refers the comment to a human reviewer.

The dashboard connects as `monitoring_ro`, a role holding `SELECT` and nothing else
(`infra/postgres-init/02-monitoring-role.sql`). It reads every number it displays out of the
database; the only files it opens are the pinned `thresholds.json` and
`baseline_flag_rates.json`, which are model artifacts fetched and digest-verified with the
model rather than metrics handed to it by another process.

### The DistilBERT challenger

The re-scorer reads the review queue and writes a second opinion onto each item, which the
reviewer console shows beside the production model's scores. The artifact is refused at load
unless it passes four gates: SHA-256 against the digest of record, `problem_type ==
"multi_label_classification"`, `id2label` in exactly the project's label order, and logit
parity against a fixture shipped with the export.

**The currently valid artifact is the float32 ONNX export.** Its int8 sibling exists and is
refused by the parity gate — the quantizer ran per-tensor and targeted the exporting host's
architecture rather than the arm64 serving fleet. Both causes are fixed but not yet proven by
a passing re-export, so the worker runs float32 until one lands. Promoting the re-export is
`CHALLENGER_MODEL_FILE` and `CHALLENGER_SHA256`, not a code change.

This README is a placeholder. The operator-facing README lands in the final phase.
