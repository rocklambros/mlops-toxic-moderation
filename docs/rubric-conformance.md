# Rubric conformance self-grade

Graded on 2026-08-10, clause by clause, against the **live** deployed system rather than
against the plan. Every clause below is transcribed from `docs/week9_FinalProject.md`, and
`tests/unit/test_rubric_matrix.py` parses that file directly — so if the assignment is
reworded, the suite goes red rather than this table quietly measuring the wrong document.

**One clause is PARTIAL and it is not a formality.** Rubric 1.3 asks for the
*best-performing* model to be promoted. The promoted model is not the best-scoring one. The
reason is good and the trade is deliberate, but a reason is a justification for PARTIAL, not
a licence to write PASS. It is the first row a grader should read.

Verdicts are PASS, PARTIAL or FAIL. There are no FAIL rows; `test_no_clause_is_left_failing`
enforces that, and a PARTIAL with no written justification also fails the suite.

## Core system requirements

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| Core 1 | `model/tracking.py` | W&B Report and registry, both verified logged out; `docs/evidence/screenshots/wandb-registry-promoted-stage.png` | PASS | |
| Core 2 | `backend/app.py` | `tests/integration/test_deployed_traversal.py` against the three live endpoints | PASS | |
| Core 3 | `backend/db.py` | RDS PostgreSQL `toxic-mod-pg`, 2033 predictions; `docs/evidence/p5-deploy-traversal.md` | PASS | |
| Core 4 | `frontend/ui.py` | `docs/evidence/screenshots/live-prototype-on-ec2.png`, a live REVIEW decision at 0.998 | PASS | |
| Core 5 | `monitoring/dashboard.py` | `docs/evidence/screenshots/monitoring-dashboard-populated.png` | PASS | |
| Core 6 | `.github/workflows/ci.yml` | `docs/evidence/ci-gate.md`; `docs/evidence/screenshots/blocked-merge.png` | PASS | |

## Phase 1 — experimentation and model management

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| 1.1 | `model/pipeline.py` | Nine runs readable anonymously; baseline and tuned pipeline in the same project | PASS | |
| 1.2 | `model/tracking.py` | Run configs carry the git SHA, hyperparameters and metrics; `MODEL_CARD.md` records the composite `data_version` | PASS | |
| 1.3 | `model/tracking.py` | `toxic-clf` at alias `production`, visible logged out; `scripts/verify_public_registry.py` returns PUBLIC AND PROMOTED | PARTIAL | The registry mechanics are complete — versioned artifact, promoted stage, public. What is short of PASS is "best-performing": the promoted classical pipeline scores macro PR-AUC **0.6632** held-out, while the DistilBERT challenger reached **0.7268** on validation (`MODEL_CARD.md` §5). The classical model is promoted because it is the one that passes the serving gate — digest-verified `skops`, CPU-servable on arm64 within the cost envelope — while the challenger's int8 export is refused at max abs logit delta 0.5728 against a 0.05 ceiling and float32 does not fit the instance budget. Deliberate, disclosed, and still not what the clause literally asks for |

## Phase 2 — backend API and database integration

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| 2.1 | `backend/app.py` | `/predict` and `/health` documented with runnable `curl` in `README.md`, both answering live | PASS | |
| 2.2 | `backend/persistence.py` | AWS RDS PostgreSQL, not a local file | PASS | |
| 2.2-log-every-request | `backend/persistence.py` | Every row carries input, output, `latency_ms`, timestamp and `persist_status`; `tests/integration/test_request_log.py` | PASS | Logging is not best-effort: `persist_status` records the outcome so a silent write failure is visible rather than absent |

## Phase 3 — frontend and live monitoring

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| 3.1 | `frontend/ui.py` | Live Streamlit UI rendering a decision and six calibrated probabilities | PASS | |
| 3.2 | `monitoring/dashboard.py` | `docs/evidence/screenshots/monitoring-dashboard-populated.png` | PASS | |
| 3.2-different-server | `infra/terraform/compute.tf` | Monitoring runs on its own `t4g.medium`, separate from backend and frontend; `docs/evidence/screenshots/aws-console-three-ec2-and-rds.png` | PASS | |
| 3.2-not-json-files | `monitoring/queries.py` | Every panel reads RDS as `monitoring_ro`; no JSON exchange exists. The role's `DELETE` and `UPDATE` are refused, verified in `tests/integration/test_deployed_traversal.py` | PASS | |
| 3.2-latency | `monitoring/queries.py` | Per-day p50 and p95 over 15 buckets, plotted as points sized by request count | PASS | The chart plots points rather than a line because an interpolated line across days with no traffic reported a flat 17 ms p50 as a 5x ramp |
| 3.2-target-drift | `monitoring/queries.py` | Predicted class distribution against the pinned baseline, with PSI and an alert threshold | PASS | Drift keeps a bounded 14-day window by design; every other panel shows all history |
| 3.2-user-feedback | `backend/feedback.py` | Agree/disagree captured from the UI, plus a reviewer queue; live accuracy is a Horvitz-Thompson estimate over the sampled queue, reported separately from self-selected user agreement | PASS | The two are reported separately because self-selected feedback is not an unbiased accuracy estimate |

## Phase 4 — testing and CI/CD automation

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| 4.1 | `tests/` | 1740 unit tests and 13 skipped, plus the integration suite | PASS | |
| 4.1-unit | `tests/unit/` | e.g. `tests/unit/test_preprocess.py`, `tests/unit/test_thresholds.py` | PASS | |
| 4.1-integration | `tests/integration/` | `tests/integration/test_predict_api.py` exercises the FastAPI endpoints with `pytest` | PASS | |
| 4.2 | `.github/workflows/ci.yml` | Triggers on pull requests to `main`, runs `ruff` and `pytest`, plus `gitleaks`, `semgrep` and a Terraform gate | PASS | |
| 4.2-blocked-merge | `.github/workflows/ci.yml` | PR #15 opened with a deliberately failing test and refused: `mergeStateStatus BLOCKED` via the API, and the CLI refused even with `--admin` | PASS | Branch protection was proven by making it refuse something, not by screenshotting a settings page |

## Phase 5 — containerization and deployment

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| 5.1 | `backend/Dockerfile` | Four images, base images pinned by digest; `tests/unit/test_dockerfile_hygiene.py` | PASS | |
| 5.2 | `infra/terraform/compute.tf` | Three EC2 instances, one component each, rolled by SSM and gated on three live health endpoints | PASS | |
| 5.3 | `README.md` | Setup, deployment, availability window, and runnable `curl` examples including the 422 and 429 paths | PASS | |

## Deliverables

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| GitHub Repository URL | `README.md` | Public repository, verified logged out on 2026-08-10 | PASS | |
| Project Workflow Screenshots | `docs/evidence/screenshots/` | AWS console showing three EC2 and RDS, plus the live prototype and the populated dashboard; declared and redaction-checked in `docs/submission-manifest.yml` | PASS | Account ids, instance ids and public addresses are filled over, never blurred |
| Experiment Tracking Dashboard URL | `docs/submission-manifest.yml` | A published W&B Report that renders with no account; `docs/evidence/screenshots/wandb-experiment-tracking-logged-out.png` | PASS | The project URL alone renders an empty workspace to a logged-out visitor. That was true until 2026-08-10 and the evidence of it is kept |

## What this self-grade does not cover

Three things outside the rubric that would be dishonest to omit from a document claiming to
grade the system. Two closed on 2026-08-10 and are struck through rather than deleted; the
open one is item 2:

1. ~~`survives_stop_start` is unverified.~~ **Verified 2026-08-10**: full stop, RDS to
   `stopped`, endpoints refusing, then back to three healthy endpoints with 2033 predictions
   intact and the Elastic IPs unmoved. `docs/evidence/p5-stop-start-cycle.md`. The cycle
   found two real defects on the way, both fixed: the dump's verification could not complete
   and could not have detected a truncated archive, and `make aws-up` could never succeed on
   this fleet.
2. **The demo window is open with no scheduled close**, so the graded listeners serve
   cleartext HTTP to the internet until someone closes them. `docs/tls-decision.md` accepts
   this and `docs/post-demo-closure.md` owns closing it.
3. ~~The SNS alert subscription is unconfirmed.~~ **Confirmed and delivering, 2026-08-10**,
   verified by publishing a test message rather than by reading the subscription state. Four
   CloudWatch alarms and the `$100` monthly budget publish to `toxic-mod-alerts`; all four
   alarms are in `OK`.
