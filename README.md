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

This README is a placeholder. The operator-facing README lands in the final phase.
