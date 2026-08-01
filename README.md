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

This README is a placeholder. The operator-facing README lands in the final phase.
