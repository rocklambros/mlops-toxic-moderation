# mlops-toxic-moderation

A multi-label toxic comment moderation service, built and operated end to end: experiment
tracking and a model registry, a FastAPI backend, a managed Postgres database, a user
interface, a monitoring dashboard on its own server, and a CI gate that blocks merges.

Six labels, independent, in this order: `toxic`, `severe_toxic`, `obscene`, `threat`,
`insult`, `identity_hate`. Trained on the Jigsaw English toxic-comment corpus.

- Experiment tracking: <https://wandb.ai/rocklambros/toxic-moderation>
- Model registry (promoted stage visible): <https://wandb.ai/rocklambros/toxic-moderation/registry>
- Model card: [`MODEL_CARD.md`](MODEL_CARD.md) · Security policy: [`SECURITY.md`](SECURITY.md)
- Design: [`docs/2026-07-01-toxic-moderation-mlops-design.md`](docs/2026-07-01-toxic-moderation-mlops-design.md)

## What runs where

| Instance | Class | Component | Port | Public URL |
|---|---|---|---|---|
| EC2 #1 | `t4g.medium` | FastAPI backend, `/predict` and `/health` | 8000 | `http://<eip-1>:8000` |
| EC2 #2 | `t4g.small` | Streamlit user interface | 8501 | `http://<eip-2>:8501` |
| EC2 #2 | `t4g.small` | Streamlit reviewer queue | 8503 | operator only, never public |
| EC2 #3 | `t4g.medium` | Monitoring dashboard | 8502 | `http://<eip-3>:8502` |
| RDS | `db.t4g.micro` | Postgres 16, private subnets | 5432 | no internet path |

Everything is Graviton (`arm64`) in `us-west-2`. There is no SSH and no open port 22;
operations run over AWS Systems Manager.

The reviewer queue on 8503 is deliberately not on that public list. It writes the metric the
dashboard is graded on, so no ingress rule of any kind carries it: it binds loopback on the
instance and is reached through an SSM port-forward session. `infra/exposure.py` is the
single source of truth for which port is which, and a test holds the Terraform to it.

## Availability window

The stack is stopped between sessions to stay inside a `$100`/month budget, so the public
URLs answer only while it is up.

**Live for grading: 2026-08-14 through 2026-08-18, 09:00–21:00 US/Mountain (UTC-6).**

Outside that window the Elastic IPs are still allocated and the addresses in this README
stay correct, but nothing listens on them. Email `rock@rockcyber.com` for a window outside
these hours and the stack comes up in about six minutes.

## Setup

Local development needs Python 3.11, Docker with Compose v2, and `make`. Nothing here
touches AWS.

```bash
git clone https://github.com/rocklambros/mlops-toxic-moderation.git
cd mlops-toxic-moderation
make venv                       # 3.11 venv, hashed lock, --require-hashes
make lint test                  # ruff + the unit suite
make data                       # deterministic split + the leakage firewall gate
```

Bring the whole stack up locally, including Postgres:

```bash
export DEMO_API_KEY="$(openssl rand -hex 16)"
export REVIEWER_SHARED_SECRET="$(openssl rand -hex 16)"
export SUBMITTER_FP_KEY="$(openssl rand -hex 16)"
docker compose -f infra/docker-compose.yml up -d --build
```

Generated rather than typed, and deliberately not printed here: a literal in a public README
is a credential somebody pastes into something that is not a laptop.

Every credential is an interpolated variable with no default, so a missing one fails the
`up` rather than starting the reviewer console on a secret nobody chose.

The user interface is then on <http://localhost:8501>, the dashboard on
<http://localhost:8502>, and the API on <http://localhost:8000>. The DistilBERT challenger
is optional and lives behind a profile: `docker compose --profile challenger up -d`.

## Example requests

`/predict` takes one comment and returns a calibrated probability and a flag for each of the
six labels, plus a moderation decision. It requires the demo API key, sent as
`X-API-Key: $DEMO_API_KEY`; the value is not published in this repository and travels with
the assignment submission. `/health` needs no key.

**A comment that is allowed:**

```bash
curl -X POST "http://<eip-1>:8000/predict" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DEMO_API_KEY" \
  -d '{"text": "thanks for the thoughtful edit, this reads much better now"}'
```

```json
{
  "request_id": "0f3c1a6e-2b5d-4a71-9f0e-6c2a4d8b1e37",
  "model_version": "toxic-clf:v3",
  "labels": {
    "toxic":         {"prob": 0.02, "flag": false},
    "severe_toxic":  {"prob": 0.00, "flag": false},
    "obscene":       {"prob": 0.01, "flag": false},
    "threat":        {"prob": 0.00, "flag": false},
    "insult":        {"prob": 0.01, "flag": false},
    "identity_hate": {"prob": 0.00, "flag": false}
  },
  "decision": "allow",
  "max_prob": 0.02,
  "latency_ms": 31
}
```

**A comment that is flagged and enqueued for human review:**

```bash
curl -X POST "http://<eip-1>:8000/predict" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DEMO_API_KEY" \
  -d '{"text": "you are an absolute clueless idiot and everyone knows it"}'
```

```json
{
  "request_id": "b81d0c44-7a90-4de2-8c19-5f7b3e2a90cc",
  "model_version": "toxic-clf:v3",
  "labels": {
    "toxic":         {"prob": 0.94, "flag": true},
    "severe_toxic":  {"prob": 0.11, "flag": false},
    "obscene":       {"prob": 0.38, "flag": false},
    "threat":        {"prob": 0.01, "flag": false},
    "insult":        {"prob": 0.89, "flag": true},
    "identity_hate": {"prob": 0.03, "flag": false}
  },
  "decision": "review",
  "max_prob": 0.94,
  "latency_ms": 34
}
```

**Rejected: the input-size cap.** The cap is `MAX_INPUT_CHARS`, which is 5000 characters,
and the request schema enforces it before the model is reached. Request bodies are
separately capped at 16 KB, and each key is limited to 30 requests per minute.

```bash
curl -i -X POST "http://<eip-1>:8000/predict" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DEMO_API_KEY" \
  -d "{\"text\": \"$(python3 -c 'print("a"*5001)')\"}"
# HTTP/1.1 422 Unprocessable Entity
```

**Rejected: no key.** Requests without a valid key are refused before the body is parsed.

```bash
curl -i -X POST "http://<eip-1>:8000/predict" \
  -H "Content-Type: application/json" -d '{"text": "hello"}'
# HTTP/1.1 401 Unauthorized
```

**Readiness, which needs no key and never returns the artifact digest:**

```bash
curl -sS "http://<eip-1>:8000/health"
```

```json
{
  "status": "ok",
  "model_version": "toxic-clf:v3",
  "database": "ok",
  "spool_depth": 0,
  "rejected": {"unauthenticated": 0, "rate_limited": 0, "oversize": 0}
}
```

`status` is `ok` only when the database answers and the spool is empty; `degraded` means the
process is serving but one of those needs attention. The deploy gate asserts `ok` on each
instance. The response carries the opaque model version only, never the artifact digest.

**Through the user interface instead.** Open `http://<eip-2>:8501`, paste a comment, press
**Check**. The decision and the six per-label probabilities render, and an agree/disagree
control writes a feedback row that the dashboard's live-accuracy panel reads.

## Deployment

Deployment is a GitHub Actions workflow. There is no manual `docker` step and no SSH.

```
push to main (code paths only)
  -> build 5 arm64 images on ubuntu-24.04-arm, tag = git SHA
  -> push to 4 ECR repositories through the gha-deploy OIDC role
  -> upload this SHA's instance scripts to s3://$DEPLOY_BUCKET/deploy/<sha>/
  -> SSM Run Command per component: bash /opt/toxic/bootstrap.sh <sha>
       assert the invocation count equals the instance count
       poll every invocation to a terminal state
       fail on anything but Success and print StandardErrorContent
  -> curl /health against all three Elastic IPs        <- the real gate
  -> record /toxic/deploy/previous-sha then current-sha
```

Infrastructure is separate and never runs unattended. `terraform apply` lives in a
manually-dispatched workflow, so a documentation commit cannot replace three instances.

Day-to-day operation:

```bash
make aws-up            # start RDS, then EC2, then the application; gate on /health
make deploy-verify     # re-run the health gate on its own
make aws-down          # pg_dump to S3 FIRST, then stop; prints the auto-restart deadline
make db-restore S3_KEY=db/2026-08-14T18-02-11Z.dump
make rollback          # re-roll the previous SHA. No Terraform. See infra/ROLLBACK.md
make aws-destroy       # full teardown; the dump is already in S3
```

`make aws-down` dumps before it stops because a stopped RDS instance **restarts by itself
after seven days**, and the alternative remedy — destroying it — would delete the dataset
the graded dashboard is built on. There is no teardown path that skips the dump.

## Repository layout

```
model/        data pipeline, leakage firewall, training, registry, thresholds
backend/      FastAPI, safe skops loader, moderation policy, persistence, retention
frontend/     Streamlit user interface and the separate reviewer queue
monitoring/   Streamlit dashboard: latency, target drift, live accuracy
rescorer/     optional DistilBERT ONNX challenger worker
infra/        Terraform, bootstrap, compose, deploy and operations scripts
scripts/      operator tools: held-out export, demo seeding, evidence redaction
docs/         design specs, plans, runbooks, evidence
```

## The demo dataset behind the dashboard

The monitoring dashboard is populated by `make seed-demo`, which replays roughly 2,000
comments from the **locked held-out split** through `/predict` and back-dates their
timestamps across 14 days. Predictions are real and their latency is measured; only the
timestamp is written by the operator tool. Every seeded row carries `predictions.is_seed =
true` and every seeded review carries `reviewer_id = 'seed-replay'`, and the dashboard
states how many of the displayed rows are seeded.

Live accuracy is a Horvitz-Thompson estimate over two probability-sampled strata: every
flagged item is reviewed, and a `RANDOM_AUDIT_RATE` fraction of the rest is audited. The
inclusion probability is stored on each `review_queue` row at enqueue time, so the estimate
stays sound when the rate is changed. User feedback from the agree/disagree control is
collected and displayed with its own interval, but is deliberately **excluded** from that
estimate: a self-selected click has no known inclusion probability, and a graded metric must
not be writable by an anonymous visitor.

The dashboard connects as `monitoring_ro`, a role holding `SELECT` and nothing else.

## Data handling and retention

`/predict` stores the submitted comment in `predictions.input_text`. A scheduled purge
nulls it after `INPUT_TEXT_RETENTION_DAYS` (default 30) and keeps the rest of the row for
monitoring. Raw comment text is never written to Weights & Biases, to application logs, or
to any screenshot in this repository. The review queue keeps its own snapshot so a purge
cannot destroy a reviewer's evidence mid-workflow, and that snapshot has its own hard TTL.

## Cost

Two numbers matter, and the hourly one is the smaller.

| | Amount | When it accrues |
|---|---|---|
| Fixed monthly | `$26.65` | **Always** — Elastic IPs on stopped instances, RDS storage and snapshots, ECR, CloudWatch Logs, S3, GuardDuty, CloudTrail |
| Variable hourly | `$0.100` | Only while the three instances and RDS are running |

Worst case, a full billing month with the stack up around the clock, is scenario C in
[`docs/cost-model.md`](docs/cost-model.md) at `$99.65`. That document prices every line item
and is the figure of record. The realistic graded fortnight, with the nightly stop schedule
in force, is scenario A at `$28.28`.

The `$100`/month budget carries alerts at 50, 80 and 100 percent, and — because an alert is
a notification and not a control — a **nightly stop** of all three instances and the
database, plus a service control policy that denies every instance type outside a four-entry
Graviton allowlist. That denial is a hard refusal, not a warning.

## Licence and provenance

Course project for COMP 4450. The Jigsaw corpus is public research data owned by others and
is not redistributed here. Model limitations, fairness measurements, and the adversarial
exposure created by publishing the registry are documented in [`MODEL_CARD.md`](MODEL_CARD.md).
