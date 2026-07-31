# Phase 5: Containerization, Three-EC2 Deployment, Documentation, Submission

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take the four component images built in Phases 2 and 3 and make them run — on three separate EC2 instances, restarted by systemd at boot, rolled by an SSM Run Command that **proves** it ran, verified by `curl /health` against three Elastic IPs, recoverable by a rehearsed one-command rollback that never touches Terraform, and survivable across a stop/start cycle and an RDS teardown. Then write the three graded and mandatory documents (`README.md`, `MODEL_CARD.md`, `SECURITY.md`) so that every present-tense claim in them is true of the system that was actually built, and hand in four deliverables verified in a logged-out browser.

**Architecture:**

```
GitHub Actions  (public repo, ubuntu-24.04-arm, OIDC -> gha-deploy)
  build 5 images / 4 ECR repos, tag = git SHA, BuildKit secret mounts only
        |
        |  aws s3 cp infra/deploy/instance/  s3://$DEPLOY_BUCKET/deploy/<sha>/
        |  (the roll script travels by S3; SendCommand carries no secret and no script body)
        v
  infra/aws/ssm_run.sh <component> <expected_count> "bash /opt/toxic/bootstrap.sh <sha>"
        |
        |  1. send-command                 -> CommandId
        |  2. list-command-invocations     -> ASSERT count == expected_count   (H5)
        |  3. get-command-invocation       -> poll each to terminal            (H5)
        |  4. anything but Success         -> print StandardErrorContent, exit 1 (H5)
        v
  +-------------------+   +--------------------+   +----------------------+
  | EC2 #1 backend    |   | EC2 #2 frontend    |   | EC2 #3 monitoring    |
  | t4g.medium  EIP#1 |   | t4g.small   EIP#2  |   | t4g.medium   EIP#3   |
  | :8000 /health     |   | :8501 user UI      |   | :8502 dashboard      |
  |                   |   | :8503 reviewer     |   | (:rescorer, profile) |
  | systemd toxic-... |   | systemd toxic-...  |   | systemd toxic-...    |
  | restart: unless-  |   | restart: unless-   |   | restart: unless-     |
  |   stopped         |   |   stopped          |   |   stopped            |
  | awslogs driver    |   | awslogs driver     |   | awslogs driver       |
  +---------+---------+   +---------+----------+   +----------+-----------+
            |                       |                         |
            +------------- 5432 ----+------- RDS (private) ---+
        v
  infra/aws/verify_deploy.sh  -- the REAL gate: three EIPs answer, no digest leaks
        v
  infra/aws/record_deploy.sh  -- /toxic/deploy/previous-sha := current, then current := new
```

Rollback is the same path with an older SHA and no Terraform:

```
infra/aws/rollback.sh [sha]
  -> read /toxic/deploy/previous-sha
  -> ASSERT every ECR repo still holds that tag (the keep-last-10 lifecycle erodes targets)
  -> ssm_run.sh (same asserted path)  -> verify_deploy.sh  -> swap the SSM parameters back
```

**Tech Stack:** Docker Engine 25+ on Amazon Linux 2023 arm64, Docker Compose v2 as a checksummed `linux-aarch64` plugin binary, systemd, AWS CLI v2 (2.36.3), GitHub Actions on `ubuntu-24.04-arm`, ECR, SSM Run Command and Parameter Store, Secrets Manager, S3, Terraform 1.15.8 with `hashicorp/aws` 6.57.1 (Phase 5 adds one file to the A2 root module), Python 3.11, pytest 8.3.3, PyYAML 6.0.2, `cyclonedx-bom` for the SBOM.

## Global Constraints

Inherited from `docs/superpowers/specs/2026-07-30-delivery-plan-design.md` (**governs on conflict**), `docs/superpowers/specs/2026-07-30-aws-account-foundation-design.md`, and the master roadmap. The ones that bind Phase 5:

- **Three EC2 instances, one component each.** Backend, frontend, monitoring. Never two. Rubric 5.1 names one container for the backend and one for the frontend; 5.2 requires "separate EC2 instances"; 3.2 requires the dashboard on "a different EC2 server".
- **Region `us-west-2`, Graviton `arm64`, AL2023.** The AWS Academy Learner Lab is dead: `LabRole`, pasted STS credentials, `us-east-1`, `vockey`, and x86 `t3` never appear in any artifact this phase produces.
- **No SSH, no port 22, no bastion, no NAT.** Every remote action is SSM Run Command or SSM Session Manager. If SSM is broken the instance is unreachable by design — `docs/runbooks/no-ssh-debug.md` (Phase A2) is the only recovery path.
- **No static AWS credentials.** GitHub Actions authenticates through OIDC into `gha-deploy`. EC2 authenticates through its per-tier instance profile. Nothing else exists.
- **No secret value crosses SendCommand.** The command text lands in CloudTrail and in `aws ssm list-commands` history in plaintext, and both are readable by anyone with read access to the account. Secrets are read **on the instance** from Secrets Manager under the instance profile.
- **`WANDB_API_KEY` reaches a build only through a BuildKit secret mount.** A `--build-arg` or an `ENV` bakes it into an image layer permanently, and these images are pushed to a registry.
- **The repository is PUBLIC.** Every Actions log, every committed document, and every screenshot is world-readable. The 12-digit AWS account id appears in ECR URIs, in `terraform output`, and in every AWS Console screenshot. It is masked in logs and redacted in screenshots.
- **The W&B Registry page is publicly visible showing a promoted stage** (owner decision, 2026-07-31). The white-box evasion this exposes is accepted residual risk and is **disclosed in the model card**. Do not plan a private artifact project.
- **Solo developer, 19 days from 2026-07-30.** Phase 5 owns days 13–16 plus the buffer. `make seed-demo` (Phase 3) and the day-9 smoke deploy (Phase A2) are prerequisites, not Phase 5 work.
- **Human author (`rocklambros <rock@rockcyber.com>`). No AI attribution in commits, code, or documentation.**
- **Feature-branch and PR.** Never commit to `main` directly.

**Branch:** `feat/phase-5-deploy-docs` off `main`.

**Operator environment:** every AWS CLI command assumes `export AWS_PROFILE=mlops-admin` and `export AWS_REGION=us-west-2` against the member account. `rc-mgmt` is the management-account profile and is never used here.

## File Structure

```
infra/deploy/
  compose.backend.yml            production compose, EC2 #1
  compose.frontend.yml           production compose, EC2 #2 (user UI + reviewer UI)
  compose.monitoring.yml         production compose, EC2 #3 (+ rescorer behind a profile)
  toxic-stack.service            systemd unit: docker compose up at boot
  Dockerfile.artifacts           optional artifact-bake image; the ONLY BuildKit secret user
  instance/
    bootstrap.sh                 what SendCommand actually runs on the box
    roll.sh                      pull by digest, write env, restart the unit
    fetch_artifacts.sh           digest-verified fetch, W&B primary, S3 mirror fallback
infra/aws/
  ssm_run.sh                     send + ASSERT count + poll to terminal + print stderr (H5)
  verify_deploy.sh               curl /health against three EIPs — the real gate (H5)
  record_deploy.sh               previous-sha then current-sha, in that order (C8)
  rollback.sh                    re-roll the previous SHA, no Terraform (C8)
  db_dump.sh                     pg_dump -> S3, through ssm_run.sh (H6, H29)
  db_restore.sh                  S3 -> pg_restore, through ssm_run.sh (H6, H29)
  aws_up.sh                      RDS, then EC2, then the APPLICATION, then verify (REG-5)
  aws_down.sh                    dump first, then stop, then record the restart deadline
infra/terraform/
  deploy.tf                      deploy bucket, endpoint + boot + deploy SSM params,
                                 instance-role policy additions   (Phase 5 owns this file)
  templates/user_data.sh.tftpl   AL2023 bootstrap: compose v2 binary, unit, boot marker
scripts/
  redact.py                      account-id and secret redaction for public artifacts
  verify_submission.py           logged-out reachability + evidence manifest checks
docs/
  submission-manifest.yml        the four deliverables and their evidence
  rubric-conformance.md          clause-keyed self-grade against week9_FinalProject.md
  evidence/
    p5-rollback-rehearsal.md     day-14 rehearsal record (C8)
    p5-deploy-traversal.md       the deployed end-to-end traversal (H26)
    screenshots/                 redacted PNGs
infra/ROLLBACK.md                rollback + RDS restore runbook (never cut)
README.md                        rubric 5.3 — setup, deployment, EXAMPLE USER REQUESTS
MODEL_CARD.md                    finalized: metrics, CIs, provenance, fairness, disclosures
SECURITY.md                      rewritten as a claim/status/evidence table (H33)
aibom.json  sbom.json            CycloneDX, SEVERABLE (cut-line item 1)
.github/workflows/deploy.yml     build, push, roll, verify, record. No terraform apply
tests/unit/
  test_readme.py test_redact.py test_compose_production.py test_systemd_unit.py
  test_user_data.py test_buildkit_secrets.py test_deploy_workflow.py
  test_model_card.py test_security_md.py test_sbom_severability.py
  test_submission_manifest.py test_rubric_matrix.py
tests/infra/
  test_deploy_tf.py test_ssm_run.py test_verify_deploy.py test_record_deploy.py
  test_rollback.py test_db_lifecycle.py test_aws_up.py test_fetch_artifacts.py
```

## Interfaces Produced

Phase 5 produces operations, not Python APIs. These are the seams.

```
# Shell entry points (all take AWS_REGION from the environment; none takes a secret argument)
infra/aws/ssm_run.sh        <component> <expected_count> <remote-command...>   -> 0 | 1 | 2
infra/aws/verify_deploy.sh                                                     -> 0 | 1
infra/aws/record_deploy.sh  <new_sha>                                          -> 0 | 1
infra/aws/rollback.sh       [target_sha]                                       -> 0 | 1
infra/aws/db_dump.sh        [label]      # prints the s3 key it wrote          -> 0 | 1
infra/aws/db_restore.sh     <s3_key>                                           -> 0 | 1
infra/aws/aws_up.sh                                                            -> 0 | 1
infra/aws/aws_down.sh                                                          -> 0 | 1

# On-instance, delivered by S3, executed by SendCommand
/opt/toxic/bootstrap.sh     <sha>        # fetch this SHA's scripts, then run roll.sh
/opt/toxic/roll.sh          <sha>        # pull by digest, write /etc/toxic/*.env, restart unit

# Make targets appended to the Phase 0/3 Makefile
make aws-up  aws-down  aws-destroy  db-dump  db-restore  rollback
make deploy-verify  evidence  sbom  aibom  submission-check  rubric-grade

# SSM Parameter Store (String, non-secret, region us-west-2)
/toxic/deploy/current-sha           the git SHA now serving
/toxic/deploy/previous-sha          the rollback target
/toxic/endpoints/{backend,frontend,monitoring}   "http://<eip>:<port>"
/toxic/boot/{backend,frontend,monitoring}        "ok <iso8601> <ami-id>"  written by user data
/toxic/ops/rds-stopped-at           ISO-8601 stamp; the 7-day auto-restart deadline (H29)

# S3, in the Phase 5 deploy bucket
s3://$DEPLOY_BUCKET/deploy/<sha>/           the instance scripts and compose files for that SHA
s3://$DEPLOY_BUCKET/artifacts/<sha256>/     the digest-pinned model artifact mirror (REG-10d)
s3://$DEPLOY_BUCKET/db/<stamp>.dump         pg_dump custom-format backups (H6, H29)

# Terraform additions in infra/terraform/deploy.tf (same root module as Phase A2)
output "deploy_bucket"      string
```

## Interfaces Consumed

| From | Name | Used for |
|---|---|---|
| Phase A2 `outputs.tf` | `instance_ids`, `ssm_target_tag` (`Component`), `ecr_repository_urls`, `log_group_names`, `db_endpoint`, `db_name`, `db_master_secret_arn`, `db_readonly_secret_arn`, `gha_deploy_role_arn`, `backend_url`, `frontend_url`, `monitoring_url` | Every script and compose file |
| Phase A2 `infra/smoke/` | the day-9 throwaway smoke deploy and `docs/evidence/a2-smoke-deploy.md` | H26: every first-time-ever integration was already exercised once |
| Phase A2 `docs/runbooks/no-ssh-debug.md` | console output, `describe-instance-information`, Serial Console | Recovery when the boot marker never appears |
| Phase 2 | `backend/Dockerfile`, `GET /health`, `MODEL_CARD.md` digest-of-record block, `MAX_INPUT_CHARS`, `X-API-Key` | Images, health gate, README examples |
| Phase 3 | `infra/docker-compose.yml` (local), `frontend/Dockerfile`, `frontend/Dockerfile.reviewer`, `monitoring/Dockerfile`, `rescorer/Dockerfile`, `infra/exposure.py`, `make seed-demo` | Production compose files, port contract, dashboard data |
| Phase 1 | `artifacts/fairness_slices.json`, `thresholds.json`, `baseline_flag_rates.json`, the W&B registry version and artifact digest | Model card fairness section, artifact fetch |

## Interface Contract corrections (premortem H24)

The master plan's Interface Contracts block is declared authoritative and has drifted. Phase 2 corrected five rows at the serving seam. These are the three that Phase 5 touches, applied to the master plan in Task 26.

| Master plan says | Corrected to | Why |
|---|---|---|
| Phase 5 heading: "Docker, **two-EC2** deploy, README, model card, AIBOM"; task 2 "Confirm or resize the **EC2 #2** instance class"; exit criteria "deploy scripts stand up **both** EC2 + RDS" | Three EC2 instances: backend `t4g.medium`, frontend `t4g.small`, monitoring `t4g.medium`. The re-scorer sizing question attaches to **EC2 #3** and is moot if the challenger is cut | H2. The three-instance decision lived in one paragraph of a document the Phase 5 implementer is never told to open, while the phase's own heading said two |
| "The pinned digest travels as `@sha256:...` in the model card and the deploy env var `MODEL_DIGEST`" | The digest of **record** is the block in the git-committed `MODEL_CARD.md`. `MODEL_DIGEST` is a *cross-check* the loader compares against the card and refuses to load on mismatch | TAIL-1 / Phase 2 D3. If the deploy environment supplies the expected digest, provenance degrades to "the thing that gave me the artifact also told me what it should hash to" |
| `infra/ROLLBACK.md` listed under "Rollback plan (`building-rollback-plan`; optional `building-canary-rollout`)" as Phase 5 task 7, and cut-line item 1 | The rollback runbook is **never cut**, is rehearsed on day 14 while the system works, and its minimum viable form is an SSM parameter plus one command that re-rolls the previous SHA's images **without touching Terraform** | C8. Rollback is not a deliverable, it is the capability that saves the deliverables, and cutting it removes recovery at exactly the moment recovery is needed |

## Premortem coverage map

Every row has an owning task whose test **fails if the finding is unfixed**. Ids beginning `REG-` are unnumbered normative items from the delivery spec that the premortem found had no schedule row and no test. `DELIV-` ids are the four submission deliverables in delivery-spec §12. `CUT-1` is the severability requirement on cut-line item 1.

| Id | Finding | Owning task | Test that fails if unfixed |
|---|---|---|---|
| H5 | `SendCommand` is fire-and-forget; a zero-instance tag match returns a CommandId and exits 0 | 11, 12 | `test_zero_matching_instances_fails_the_deploy`, `test_failed_invocation_prints_standard_error_and_fails`, `test_verify_fails_when_one_endpoint_is_down` |
| H26 | Days 13–14 is the first moment ECR auth, arm64 boot, digest-verified fetch, instance-to-instance HTTP, RDS, EIP association and the SSM roll all run for the first time | 5, 6, 7, 12, 20 | `test_user_data_installs_compose_v2_as_a_checksummed_binary`, `test_boot_marker_is_the_last_action`, `test_traversal_evidence_cites_the_day_9_smoke_deploy` |
| C8 | The cut-line cannot fire in time, and the rollback runbook was item 1 on it | 14, 15, 16 | `test_rollback_never_invokes_terraform`, `test_previous_sha_is_recorded_before_current_sha`, `test_rehearsal_evidence_is_dated_and_complete` |
| H6 | `terraform destroy` fails on `aws_db_instance` without a snapshot; `skip_final_snapshot = true` permanently deletes the graded dashboard dataset | 17, 18 | `test_aws_down_dumps_before_it_stops_anything`, `test_final_snapshot_is_not_skipped` |
| H29 | A stopped RDS instance restarts automatically after 7 days; the documented remedy (destroy) deletes the graded dataset | 17, 18 | `test_aws_down_records_the_auto_restart_deadline`, `test_db_restore_round_trips_a_dump` |
| REG-5 | Nothing starts containers on a stop/start cycle; delivery-spec §12 requires the live URL reachable **after** a stop/start | 3, 4, 19 | `test_every_production_service_restarts_unless_stopped`, `test_unit_is_enabled_for_multi_user_target`, `test_aws_up_gates_on_health_not_on_instance_state` |
| REG-6.3f | `WANDB_API_KEY` must reach a build only through BuildKit secret mounts, and the deploy-time fetch must read Secrets Manager on the instance, not travel as a SendCommand parameter | 8, 10 | `test_no_build_arg_or_env_carries_the_wandb_key`, `test_send_command_payload_contains_no_secret_value` |
| REG-10d | A W&B outage at bring-up turns the fail-closed loader into a demo outage | 9 | `test_mirror_is_used_when_wandb_fails_and_the_digest_still_gates` |
| H35 | Unpinned third-party Actions can mint the `gha-deploy` OIDC token; repo default permission is *write*; unpinned base images defeat SHA traceability | 13 | `test_every_action_is_pinned_to_a_full_commit_sha`, `test_top_level_permissions_are_empty` |
| C7 | `terraform apply` runs unattended on every push to `main`, including a README typo | 13 | `test_deploy_workflow_never_runs_terraform_apply`, `test_docs_only_pushes_cannot_trigger_deploy` |
| H27 | No container logs leave the box; no log driver is configured anywhere | 3 | `test_every_production_service_ships_logs_to_cloudwatch` |
| H32 | The README is graded (5.3), is a placeholder on a public repo, is scheduled for day 15, and omits "example user requests" | 1 | `test_readme_shows_a_runnable_predict_example`, `test_readme_states_the_availability_window` |
| H31 | Zero fairness measurement for a Jigsaw content-moderation classifier; `SECURITY.md` cites a `MODEL_CARD.md` that does not exist | 21 | `test_model_card_fairness_section_matches_the_measured_slices` |
| H13 / TAIL-1 | The public registry hands out the exact coefficient vector; the digest and artifact share one credential | 21 | `test_model_card_discloses_the_public_registry_evasion_exposure` |
| H33 | Nine present-tense claims in `SECURITY.md` are false today, and two are contradicted by the plan itself | 22 | `test_every_practice_row_has_a_status_and_evidence`, `test_the_two_contradicted_claims_are_corrected` |
| DELIV-3 | The account id appears in ECR URIs and `terraform output` in world-readable Actions logs, and in every Console screenshot | 2, 13, 24 | `test_redact_masks_an_account_id_inside_an_arn`, `test_account_id_is_masked_before_the_first_ecr_step` |
| DELIV-1..4 | Four deliverables, verified in a **logged-out** browser | 24 | `test_manifest_covers_all_four_deliverables`, `test_no_evidence_file_contains_an_account_id` |
| C9 / H34 | The coverage matrix validates the plan against the design, not against the rubric; no day is allocated to a rubric self-grade | 25 | `test_every_rubric_clause_has_an_owner_and_evidence` (clauses parsed live from `docs/week9_FinalProject.md`) |
| CUT-1 | AIBOM and SBOM are cut-line item 1 | 23 | `test_no_gate_target_depends_on_the_sbom` |
| H24 | Interface Contracts drift at the three seams Phase 5 touches | 26 | Task 26 reconciliation checklist |
| H7 (the half nobody reads) | The cost model is correct and the README quotes an hourly rate only, omitting the fixed monthly charge; the delivery spec still carries the superseded `$0.101/hr` | **1a** | `test_readme_cost_agrees_with_the_cost_model`, `test_the_delivery_spec_no_longer_carries_the_superseded_hourly_figure` |
| DRIFT-ARTIFACTS | `roll.sh` names `/artifacts/thresholds.json` and `/artifacts/baseline_flag_rates.json`; `fetch_artifacts.sh` fetches neither and the `monitoring)` branch never calls it. First-boot outage on rubric 3.2 and 2.1 | **10a** | `test_the_fetcher_installs_thresholds_and_the_drift_baseline`, `test_every_artifact_path_written_into_an_env_file_is_actually_fetched` |
| SCHEMA-PROD | `apply_phase3_schema` is called only from `tests/integration/conftest.py`; production RDS has three bare tables | **19a** | `test_aws_up_applies_the_full_schema_before_it_verifies_health`, `test_the_deployed_database_carries_every_phase3_column` |
| C5 (production half) | Density is measured locally and *declared* in hand-typed YAML for production | **20b** | `test_production_holds_at_least_two_thousand_predictions`, `test_the_manifest_density_matches_the_measured_production_counts` |
| H15 / H13 (closure half) | The compensating controls both accepted risks name — close `demo_cidrs`, rotate the reviewer secret, rotate the demo API key — have no owner, no target and no test | **24a** | `test_every_control_is_recorded_closed_before_submission`, `test_every_compensating_control_the_card_claims_is_verified_in_the_manifest` |

**Explicitly not owned by Phase 5**, listed so the gap is visible rather than assumed: H15 (the TLS *decision* — Phase A2's `docs/tls-decision.md`; Phase 5 owns closing the controls it depends on, Task 24a), H16 (per-tier security groups and the read-only DB role — Phase A2), H7 (the cost *model* — Phase A2's `docs/cost-model.md`; Phase 5 owns getting its number into the README, Task 1a), C6 (explicit egress and the no-SSH runbook — Phase A2), H10 (branch protection and the blocked-merge screenshot — Phase 4; Phase 5 only verifies the evidence exists), H11 (the registry page is publicly visible — Phase 1; Phase 5 verifies it logged out), H12 (reviewer UI on its own port — Phase 3's `infra/exposure.py`, which Phase 5's compose files must obey).

## Design decisions this phase must make explicitly

**D1 — SendCommand carries a command, never a script and never a secret.** The full command text is recorded in CloudTrail and returned by `aws ssm list-commands` to anyone with read access. So the payload is exactly `bash /opt/toxic/bootstrap.sh <sha>`, and everything interesting lives in a script that the deploy job uploaded to S3 under that SHA. Three consequences fall out for free: the audit record is short and readable, rollback is "run the previous SHA's script" rather than "reconstruct the previous SHA's command", and no `${{ secrets.* }}` can be interpolated into a place where it would be logged.

**D2 — `restart: unless-stopped` is necessary and not sufficient (REG-5).** Docker restarts `unless-stopped` containers when the daemon starts, so a stop/start cycle does bring back containers *that already exist and were not deliberately stopped*. It does nothing on a replaced instance, nothing after a `docker compose down` (which is what a rollback does), and nothing if the compose project was never materialised. The systemd unit is the part that makes bring-up idempotent from any state. Both are required, and both are tested.

**D3 — the deploy gate is HTTP, not SSM (H5).** An SSM invocation reporting `Success` means a shell exited 0 on a box. It does not mean the container came up, the artifact verified, the database was reachable, or the security group allows the grader in. `verify_deploy.sh` curling three Elastic IPs is the only statement worth making, and it is what fails the job.

**D4 — `aws-down` always dumps first (H6, H29).** The two documented behaviours were mutually exclusive: "stop between sessions" collides with a 7-day auto-restart, and the documented remedy "destroy rather than stop" deletes the graded dashboard dataset that rubric 3.2 is scored on. Resolution: `make aws-down` has `db-dump` as a hard Make prerequisite, so no teardown path exists that does not produce a restorable dump in S3 first; `aws_down.sh` then records the auto-restart deadline in SSM and prints it; and `make db-restore` puts the dataset back. `skip_final_snapshot` stays `false` so `terraform destroy` also leaves a snapshot, but the pg_dump is the artifact the restore actually uses, because it is portable across a destroyed instance identifier.

**D5 — the fallback mirror is digest-pinned, not merely a second source (REG-10d).** The fail-closed loader is the trust boundary and it does not degrade. A mirror that is trusted because it is "ours" would convert a W&B outage into a weaker security posture. So the mirror key is `artifacts/<sha256>/<filename>` — the digest from the git-committed model card **is the lookup key**, which means a tampered mirror object cannot be found under the name the fetcher asks for, and if it is, the same `sha256sum -c` gate rejects it.

**D6 — the availability window is a documented promise, not a hope.** The stack's *variable* cost is about `$0.10/hr` while it is running, on top of a fixed monthly charge that accrues even while it is stopped (`docs/cost-model.md` is the figure of record; quoting the hourly rate alone is the H7 understatement, see Task 1a). It is stopped between sessions, so an Elastic IP URL is reachable only when it is up. The README states a specific window (a date range and a "email for a window outside these hours" line), and `make aws-up` is the one command that makes the window true and proves it with `verify_deploy.sh`.

---

### Task 1 (H32): README skeleton, written FIRST, with runnable example user requests

Rubric 5.3 grades setup instructions, deployment steps, **and example user requests**. The last was missing from this task's predecessor entirely, and the whole task was scheduled for day 15, where it competes with screenshots and the model card. It moves to the front of the phase so the graded document exists before anything can compress it.

**Files:**
- Modify: `README.md`
- Test: `tests/unit/test_readme.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_readme.py`:
```python
"""Rubric 5.3 is graded on three things. Two of them were in the plan; the third was not."""

import re
from pathlib import Path

README = Path("README.md")


def _text() -> str:
    return README.read_text(encoding="utf-8")


def _sections() -> list[str]:
    return [line.lstrip("# ").strip() for line in _text().splitlines() if line.startswith("## ")]


def test_readme_covers_the_three_graded_headings():
    sections = _sections()
    for required in ("Setup", "Deployment", "Example requests"):
        assert any(required.lower() in s.lower() for s in sections), f"missing '{required}' section"


def test_readme_shows_a_runnable_predict_example():
    """H32. 'Example user requests' is a rubric clause with no owning task before this one."""
    body = _text()
    assert "curl -X POST" in body
    assert "/predict" in body
    assert "X-API-Key" in body, "the example must show the demo key header or it does not run"
    assert '"text"' in body, "the example must show the request body"


def test_readme_shows_the_expected_predict_response():
    body = _text()
    for key in ("request_id", "model_version", "decision", "max_prob", "latency_ms"):
        assert key in body, f"the documented response is missing {key}"


def test_readme_shows_a_health_example():
    assert re.search(r"curl\s+(-\S+\s+)*\S*/health", _text())


def test_readme_states_the_availability_window():
    """Delivery spec section 12: the live URL carries its availability window in the README."""
    body = _text()
    assert "Availability window" in body
    assert re.search(r"20\d\d-\d\d-\d\d", body), "state real dates, not 'during work sessions'"


def test_readme_documents_the_three_instance_topology():
    body = _text()
    for component in ("backend", "frontend", "monitoring"):
        assert component in body.lower()
    assert "t4g.medium" in body and "t4g.small" in body


def test_readme_carries_no_account_id_and_no_secret_value():
    body = _text()
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", body), "a 12-digit account id is in the README"
    assert "AKIA" not in body
    for placeholder in ("$DEMO_API_KEY", "<account-id>"):
        pass  # placeholders are how the key and the id are referenced instead
    assert "DEMO_API_KEY" in body, "reference the key by variable, never by value"


def test_readme_is_not_the_placeholder():
    assert "This README is a placeholder" not in _text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_readme.py -v`
Expected: 8 failures, the first being `test_readme_covers_the_three_graded_headings` with `AssertionError: missing 'Setup' section`.

- [ ] **Step 3: Write minimal implementation**

`README.md`:
````markdown
# mlops-toxic-moderation

A multi-label toxic comment moderation service, built and operated end to end: experiment
tracking and a model registry, a FastAPI backend, a managed Postgres database, a user
interface, a monitoring dashboard on its own server, and a CI gate that blocks merges.

Six labels, independent, in this order: `toxic`, `severe_toxic`, `obscene`, `threat`,
`insult`, `identity_hate`. Trained on the Jigsaw English toxic-comment corpus.

- Experiment tracking: <https://wandb.ai/rocklambros/toxic-moderation>
- Model registry (promoted stage visible): <https://wandb.ai/rocklambros/toxic-moderation/registry>
- Model card: [`MODEL_CARD.md`](MODEL_CARD.md) · Security policy: [`SECURITY.md`](SECURITY.md)

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
export DEMO_API_KEY=local-dev-key
export REVIEWER_SHARED_SECRET=local-dev-secret
export SUBMITTER_FP_KEY=local-dev-fp-key
docker compose -f infra/docker-compose.yml up -d --build
```

The user interface is then on <http://localhost:8501>, the dashboard on
<http://localhost:8502>, and the API on <http://localhost:8000>. The DistilBERT challenger
is optional and lives behind a profile: `docker compose --profile challenger up -d`.

## Example requests

`/predict` takes one comment and returns a calibrated probability and a flag for each of the
six labels, plus a moderation decision. It requires the demo API key in an `X-API-Key`
header; the key is not published in this repository and is supplied with the submission.
`/health` needs no key.

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

**Rejected: the input-size cap.** Comments longer than 4000 characters are refused before
the model sees them.

```bash
curl -i -X POST "http://<eip-1>:8000/predict" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $DEMO_API_KEY" \
  -d "{\"text\": \"$(python3 -c 'print("a"*4001)')\"}"
# HTTP/1.1 422 Unprocessable Entity
```

**Rejected: no key.**

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
{"status": "ok", "model_version": "toxic-clf:v3", "database": "ok", "spool_depth": 0}
```

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
docs/         design specs, plans, runbooks, evidence
```

## Data handling and retention

`/predict` stores the submitted comment in `predictions.input_text`. A scheduled purge
nulls it after `INPUT_TEXT_RETENTION_DAYS` (default 30) and keeps the rest of the row for
monitoring. Raw comment text is never written to Weights & Biases, to application logs, or
to any screenshot in this repository. The review queue keeps its own snapshot so a purge
cannot destroy a reviewer's evidence mid-workflow, and that snapshot has its own hard TTL.

## Cost

About `$0.10/hour` with all three instances and RDS running, against a `$100`/month budget
with alerts at 50, 80, and 100 percent. A service control policy denies every instance type
outside a four-entry Graviton allowlist, which is a hard denial rather than an alert.

## Licence and provenance

Course project for COMP 4450. The Jigsaw corpus is public research data owned by others and
is not redistributed here. Model limitations, fairness measurements, and the adversarial
exposure created by publishing the registry are documented in [`MODEL_CARD.md`](MODEL_CARD.md).
````

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_readme.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add README.md tests/unit/test_readme.py
git commit -m "Write the operator README with runnable predict examples and an availability window"
```

---

### Task 1a (H7): The README's cost figure is the cost model's figure, and the superseded one is deleted at source

A2 Task 16 genuinely closes the cost **model**: `docs/cost-model.md` prices all fourteen previously omitted line items and `test_cost_model_prices_every_previously_omitted_line_item` fails if any is dropped. The corrected number then reaches neither document anyone actually reads.

- Task 1's README Cost section says "About `$0.10/hour` with all three instances and RDS running, against a `$100`/month budget" — the **variable subtotal only**. That is precisely the framing H7 called a ~15% understatement, and it omits the fixed monthly charge that accrues while everything is stopped: Elastic IPs on stopped instances, RDS storage, snapshots, ECR storage, CloudWatch Logs, S3, GuardDuty, CloudTrail. `tests/unit/test_readme.py` has eight assertions and none of them touch cost.
- `docs/superpowers/specs/2026-07-30-delivery-plan-design.md:85` still asserts "Roughly `$0.101/hr` with everything running". A2 Task 16 says its document "replaces the previous figure", but **no task edits that line**. A superseding document plus an unedited original is exactly the supersession-table pattern remediation 0.2 rejected: a subagent reading a narrow slice reads the stale number.

An hourly rate is also the wrong unit for the decision the reader is making. The question is "can this run for the graded fortnight inside $100", and that is a monthly scenario, not an hour.

**Files:**
- Modify: `README.md` (Cost section), `docs/superpowers/specs/2026-07-30-delivery-plan-design.md`
- Test: `tests/unit/test_readme.py` (append), `tests/infra/test_docs_controls.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_readme.py`:
```python
import re
from pathlib import Path

COST_MODEL = Path("docs/cost-model.md")


def test_readme_cost_agrees_with_the_cost_model():
    """H7. An hourly rate for the running state omits the money that accrues while the
    stack is stopped, which is most of the graded fortnight."""
    body = _text()
    model = COST_MODEL.read_text(encoding="utf-8")
    fixed = re.search(r"Fixed monthly subtotal.*?\$([0-9.]+)", model, re.S)
    assert fixed, "docs/cost-model.md must state a 'Fixed monthly subtotal'"
    assert fixed.group(1) in body, "the README omits the fixed monthly cost that accrues while stopped"
    assert "$0.101" not in body
    assert re.search(r"Scenario C|full billing month|worst case", body), (
        "state the worst-case month, not just the hourly rate"
    )


def test_readme_names_the_budget_and_the_hard_control_not_only_the_alert():
    body = _text()
    assert "$100" in body
    assert "nightly stop" in body.lower() or "stops the instances" in body.lower(), (
        "a budget alert is a notification; name the control that actually stops spend"
    )
```

Append to `tests/infra/test_docs_controls.py`:
```python
def test_the_delivery_spec_no_longer_carries_the_superseded_hourly_figure():
    """Remediation 0.2: corrections are made at source. A superseding document plus an
    unedited original is a supersession table, which is what that remediation rejected."""
    spec = Path("docs/superpowers/specs/2026-07-30-delivery-plan-design.md").read_text(encoding="utf-8")
    assert "$0.101/hr" not in spec
    assert "docs/cost-model.md" in spec, "point the reader at the document of record"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_readme.py tests/infra/test_docs_controls.py -v`
Expected: FAIL — `AssertionError: the README omits the fixed monthly cost that accrues while stopped` and `assert '$0.101/hr' not in '…'`.

- [ ] **Step 3: Write minimal implementation**

Replace the README `## Cost` section (Task 1, Step 3) with:

````markdown
## Cost

Two numbers matter, and the hourly one is the smaller.

| | Amount | When it accrues |
|---|---|---|
| Fixed monthly | `$<Fixed monthly subtotal from docs/cost-model.md>` | **Always** — Elastic IPs on stopped instances, RDS storage and snapshots, ECR, CloudWatch Logs, S3, GuardDuty, CloudTrail |
| Variable hourly | `$<variable subtotal>` | Only while the three instances and RDS are running |

Worst case, a full billing month with the stack up around the clock, is scenario C in
[`docs/cost-model.md`](docs/cost-model.md), which prices every line item and is the figure of
record. The realistic graded fortnight, with the nightly stop schedule in force, is scenario A.

The `$100`/month budget carries alerts at 50, 80 and 100 percent, and — because an alert is a
notification and not a control — a **nightly stop** of all three instances, plus a service
control policy that denies every instance type outside a four-entry Graviton allowlist. That
denial is a hard refusal, not a warning.
````

Fill both figures from `docs/cost-model.md` at execution time; the test compares them, so a stale paste fails.

In `docs/superpowers/specs/2026-07-30-delivery-plan-design.md`, replace the line "Roughly `$0.101/hr` with everything running" with: "Costed in full in [`docs/cost-model.md`](../../cost-model.md), which prices the fixed monthly charges that accrue while the stack is stopped as well as the per-hour running rate. An earlier draft of this line quoted `$0.101/hr` as if it were the whole cost; that was the variable subtotal only (premortem H7)."

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_readme.py tests/infra/test_docs_controls.py -v`
Expected: 10 PASS in `test_readme.py`.

Ordering note: `docs/cost-model.md` is written by A2 Task 16, which runs before this phase. If this task is executed out of order, write the README section last.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/superpowers/specs/2026-07-30-delivery-plan-design.md \
        tests/unit/test_readme.py tests/infra/test_docs_controls.py
git commit -m "Make the README cost the cost model's cost and delete the superseded hourly figure"
```

---

### Task 2 (DELIV-3): Account-id and secret redaction for public artifacts

The 12-digit account id appears in every ECR URI, every role ARN, every `terraform output`,
and every AWS Console screenshot. The repository is public and the submission checklist says
no account id may be visible. One utility does the redaction, and everything that produces a
public artifact runs through it.

**Files:**
- Create: `scripts/__init__.py`, `scripts/redact.py`
- Test: `tests/unit/test_redact.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_redact.py`:
```python
import subprocess
import sys
from pathlib import Path

from scripts.redact import SECRET_SHAPES, redact, scan


def test_redact_masks_a_bare_account_id():
    assert redact("account 123456789012 created") == "account <account-id> created"


def test_redact_masks_an_account_id_inside_an_arn():
    """DELIV-3. The id almost never appears bare; it appears inside an ARN or an ECR URI."""
    src = "123456789012.dkr.ecr.us-west-2.amazonaws.com/toxic-backend:abc123"
    assert redact(src) == "<account-id>.dkr.ecr.us-west-2.amazonaws.com/toxic-backend:abc123"
    arn = "arn:aws:iam::123456789012:role/gha-deploy"
    assert redact(arn) == "arn:aws:iam::<account-id>:role/gha-deploy"


def test_redact_leaves_other_numbers_alone():
    assert redact("latency_ms 1234 and epoch 1754000000000000") == (
        "latency_ms 1234 and epoch 1754000000000000"
    )
    assert redact("2026-08-18") == "2026-08-18"


def test_redact_masks_known_secret_shapes():
    assert "AKIAIOSFODNN7EXAMPLE" not in redact("key AKIAIOSFODNN7EXAMPLE here")
    assert "<aws-access-key-id>" in redact("key AKIAIOSFODNN7EXAMPLE here")
    assert "<github-token>" in redact("ghp_0123456789abcdefghijklmnopqrstuvwxyzA")


def test_scan_reports_findings_with_line_numbers(tmp_path):
    target = tmp_path / "evidence.md"
    target.write_text("clean line\nrole arn:aws:iam::123456789012:role/x\n", encoding="utf-8")
    findings = scan([target])
    assert len(findings) == 1
    assert findings[0].path == target
    assert findings[0].line_number == 2
    assert findings[0].kind == "account-id"


def test_scan_returns_nothing_for_a_clean_file(tmp_path):
    target = tmp_path / "clean.md"
    target.write_text("no identifiers here\n", encoding="utf-8")
    assert scan([target]) == []


def test_secret_shapes_cover_every_credential_this_project_holds():
    covered = {name for name, _pattern, _mask in SECRET_SHAPES}
    assert {"aws-access-key-id", "github-token", "wandb-key", "bearer-token"} <= covered


def test_cli_masks_stdin_and_exits_zero(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.redact"],
        input="arn:aws:iam::123456789012:role/gha-deploy\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "123456789012" not in proc.stdout
    assert "<account-id>" in proc.stdout


def test_cli_scan_mode_exits_nonzero_on_a_finding(tmp_path):
    target = tmp_path / "bad.md"
    target.write_text("arn:aws:iam::123456789012:role/x\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.redact", "--scan", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "account-id" in proc.stdout + proc.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_redact.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'scripts.redact'`

- [ ] **Step 3: Write minimal implementation**

`scripts/__init__.py`: empty file.

`scripts/redact.py`:
```python
"""Redact the AWS account id and known credential shapes from anything that goes public.

The repository is public, GitHub Actions logs on a public repository are world-readable,
and the submission checklist requires that no account id appear in any screenshot or
evidence file. This module is the single place that knows what has to disappear.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# A 12-digit run that is not part of a longer digit run. Anchored this way it catches the
# id inside an ARN ("iam::123456789012:role") and inside an ECR URI, which is where it
# actually appears, without touching timestamps, latencies, or ISO dates.
ACCOUNT_ID = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")

SECRET_SHAPES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "<aws-access-key-id>"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"), "<github-token>"),
    ("wandb-key", re.compile(r"\b[0-9a-f]{40}\b"), "<wandb-key>"),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "Bearer <token>"),
)


@dataclass(frozen=True)
class Finding:
    path: Path
    line_number: int
    kind: str
    line: str


def redact(text: str) -> str:
    """Return `text` with the account id and every known credential shape masked."""
    out = ACCOUNT_ID.sub("<account-id>", text)
    for _name, pattern, mask in SECRET_SHAPES:
        out = pattern.sub(mask, out)
    return out


def _kinds_in(line: str) -> list[str]:
    kinds = ["account-id"] if ACCOUNT_ID.search(line) else []
    kinds += [name for name, pattern, _mask in SECRET_SHAPES if pattern.search(line)]
    return kinds


def scan(paths: list[Path]) -> list[Finding]:
    """Report every line in every path that would leak an identifier if published."""
    findings: list[Finding] = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            for kind in _kinds_in(line):
                findings.append(Finding(path=path, line_number=number, kind=kind, line=line))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="report instead of rewriting")
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)

    if args.scan:
        findings = scan(args.paths)
        for finding in findings:
            print(
                f"{finding.path}:{finding.line_number}: {finding.kind}: "
                f"{redact(finding.line).strip()}"
            )
        return 1 if findings else 0

    if args.paths:
        for path in args.paths:
            path.write_text(redact(path.read_text(encoding="utf-8")), encoding="utf-8")
        return 0

    sys.stdout.write(redact(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_redact.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/redact.py tests/unit/test_redact.py
git commit -m "Add account-id and credential redaction for public artifacts"
```

---

### Task 3 (REG-5, H27): Production compose files with restart policies and CloudWatch logging

Phase 3's `infra/docker-compose.yml` builds from source and is for a laptop. Production is
three separate hosts pulling immutable images from ECR. Two defects close here at once:
nothing restarted containers on a stop/start cycle, and no container log ever left the box.

**Files:**
- Create: `infra/deploy/compose.backend.yml`, `infra/deploy/compose.frontend.yml`, `infra/deploy/compose.monitoring.yml`
- Test: `tests/unit/test_compose_production.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_compose_production.py`:
```python
"""The production compose files are what a stopped instance comes back to."""

from pathlib import Path

import yaml

from infra.exposure import DEMO_EXPOSED_PORTS, OPERATOR_ONLY_PORTS

FILES = {
    "backend": Path("infra/deploy/compose.backend.yml"),
    "frontend": Path("infra/deploy/compose.frontend.yml"),
    "monitoring": Path("infra/deploy/compose.monitoring.yml"),
}


def _load(name: str) -> dict:
    return yaml.safe_load(FILES[name].read_text(encoding="utf-8"))


def _all_services() -> dict[str, dict]:
    services: dict[str, dict] = {}
    for name in FILES:
        services.update(_load(name)["services"])
    return services


def test_every_component_has_its_own_compose_file():
    for name, path in FILES.items():
        assert path.exists(), f"{name} has no production compose file"


def test_every_production_service_restarts_unless_stopped():
    """REG-5. Without this, a stop/start cycle leaves the instance up and the app down."""
    for service_name, spec in _all_services().items():
        assert spec.get("restart") == "unless-stopped", f"{service_name} has no restart policy"


def test_every_production_service_ships_logs_to_cloudwatch():
    """H27. No container log leaves the box today, and nothing pages when /predict is down."""
    for service_name, spec in _all_services().items():
        logging = spec.get("logging", {})
        assert logging.get("driver") == "awslogs", f"{service_name} has no awslogs driver"
        options = logging["options"]
        assert options["awslogs-group"].startswith("${LOG_GROUP")
        assert options["awslogs-region"].startswith("${AWS_REGION")
        assert options["awslogs-create-group"] == "false", "the group is Terraform's, not Docker's"


def test_no_production_service_builds_from_source():
    for service_name, spec in _all_services().items():
        assert "build" not in spec, f"{service_name} builds on the instance instead of pulling"
        assert "image" in spec


def test_every_image_is_pinned_by_digest_through_an_environment_variable():
    """The roll script resolves the tag to a digest; the compose file never floats a tag."""
    for service_name, spec in _all_services().items():
        image = spec["image"]
        assert image.startswith("${") and image.endswith("}"), service_name
        assert "IMAGE" in image, f"{service_name} image variable is misnamed: {image}"


def test_the_backend_file_holds_only_the_backend():
    assert set(_load("backend")["services"]) == {"backend"}


def test_the_frontend_file_holds_the_user_ui_and_the_reviewer_ui():
    assert set(_load("frontend")["services"]) == {"frontend", "reviewer"}


def test_the_monitoring_file_holds_the_dashboard_and_the_severable_rescorer():
    services = _load("monitoring")["services"]
    assert set(services) == {"monitoring", "rescorer"}
    assert services["rescorer"]["profiles"] == ["challenger"], "the challenger must stay severable"


def test_the_reviewer_ui_binds_loopback_only():
    """H12 and Phase 3's exposure contract: 8503 is never carried by the demo toggle."""
    reviewer = _load("frontend")["services"]["reviewer"]
    assert reviewer["ports"] == ["127.0.0.1:8503:8503"]
    assert 8503 in OPERATOR_ONLY_PORTS and 8503 not in DEMO_EXPOSED_PORTS


def test_the_publicly_bound_ports_are_exactly_the_graded_surface():
    published = set()
    for spec in _all_services().values():
        for mapping in spec.get("ports", []):
            parts = mapping.split(":")
            if len(parts) == 2:
                published.add(int(parts[1]))
    assert published == DEMO_EXPOSED_PORTS


def test_no_compose_file_contains_a_secret_value():
    for path in FILES.values():
        body = path.read_text(encoding="utf-8")
        for line in body.splitlines():
            if "SECRET" in line or "API_KEY" in line or "PASSWORD" in line:
                assert "${" in line, f"literal credential in {path}: {line.strip()}"


def test_every_service_declares_a_healthcheck_or_is_a_worker():
    services = _all_services()
    for name in ("backend", "frontend", "reviewer", "monitoring"):
        assert "healthcheck" in services[name], f"{name} has no healthcheck"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_compose_production.py -v`
Expected: FAIL — `test_every_component_has_its_own_compose_file` with `AssertionError: backend has no production compose file`.

- [ ] **Step 3: Write minimal implementation**

`infra/deploy/compose.backend.yml`:
```yaml
# EC2 #1. Pulled by /opt/toxic/roll.sh, started by the toxic-stack systemd unit.
# Every value that varies comes from /etc/toxic/stack.env, which roll.sh writes.
name: toxic-backend

services:
  backend:
    image: ${BACKEND_IMAGE}
    restart: unless-stopped
    env_file:
      - /etc/toxic/backend.env
    volumes:
      - /var/lib/toxic/artifacts:/artifacts:ro
      - /var/lib/toxic/spool:/var/lib/toxic
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_REGION}
        awslogs-group: ${LOG_GROUP_BACKEND}
        awslogs-stream: backend
        awslogs-create-group: "false"
```

`infra/deploy/compose.frontend.yml`:
```yaml
# EC2 #2. Two Streamlit processes from the same repository, different entry points.
# 8501 is the graded user interface. 8503 is the reviewer queue and binds loopback only,
# reached through an SSM port-forward session; it is never carried by the demo ingress
# toggle, because opening it would hand the graded feedback metric to any visitor.
name: toxic-frontend

services:
  frontend:
    image: ${FRONTEND_IMAGE}
    restart: unless-stopped
    env_file:
      - /etc/toxic/frontend.env
    ports:
      - "8501:8501"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_REGION}
        awslogs-group: ${LOG_GROUP_FRONTEND}
        awslogs-stream: frontend
        awslogs-create-group: "false"

  reviewer:
    image: ${REVIEWER_IMAGE}
    restart: unless-stopped
    env_file:
      - /etc/toxic/frontend.env
    ports:
      - "127.0.0.1:8503:8503"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8503/_stcore/health').status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_REGION}
        awslogs-group: ${LOG_GROUP_FRONTEND}
        awslogs-stream: reviewer
        awslogs-create-group: "false"
```

`infra/deploy/compose.monitoring.yml`:
```yaml
# EC2 #3. Rubric 3.2 requires the dashboard on a different EC2 server, so this file is the
# only thing that runs here. The DistilBERT challenger is cut-line item 3 and stays behind
# a profile, so cutting it is a one-line change with no Terraform edit.
name: toxic-monitoring

services:
  monitoring:
    image: ${MONITORING_IMAGE}
    restart: unless-stopped
    env_file:
      - /etc/toxic/monitoring.env
    volumes:
      - /var/lib/toxic/artifacts:/artifacts:ro
    ports:
      - "8502:8502"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8502/_stcore/health').status == 200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_REGION}
        awslogs-group: ${LOG_GROUP_MONITORING}
        awslogs-stream: monitoring
        awslogs-create-group: "false"

  rescorer:
    profiles: ["challenger"]
    image: ${RESCORER_IMAGE}
    restart: unless-stopped
    env_file:
      - /etc/toxic/rescorer.env
    volumes:
      - /var/lib/toxic/artifacts:/artifacts:ro
    logging:
      driver: awslogs
      options:
        awslogs-region: ${AWS_REGION}
        awslogs-group: ${LOG_GROUP_RESCORER}
        awslogs-stream: rescorer
        awslogs-create-group: "false"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_compose_production.py -v`
Expected: 12 PASS

Then prove the files are valid compose, with the variables supplied:
```bash
env BACKEND_IMAGE=x FRONTEND_IMAGE=x REVIEWER_IMAGE=x MONITORING_IMAGE=x RESCORER_IMAGE=x \
    AWS_REGION=us-west-2 LOG_GROUP_BACKEND=g LOG_GROUP_FRONTEND=g LOG_GROUP_MONITORING=g \
    LOG_GROUP_RESCORER=g \
  sh -c 'for f in infra/deploy/compose.*.yml; do docker compose -f "$f" config --quiet && echo "ok $f"; done'
```
Expected: `ok infra/deploy/compose.backend.yml`, and the same for the other two.

- [ ] **Step 5: Commit**

```bash
git add infra/deploy/compose.backend.yml infra/deploy/compose.frontend.yml \
        infra/deploy/compose.monitoring.yml tests/unit/test_compose_production.py
git commit -m "Add per-instance production compose with restart policies and CloudWatch logging"
```

---

### Task 4 (REG-5): A systemd unit that brings the stack up at boot

`restart: unless-stopped` handles a daemon restart. It does nothing on a replaced instance,
nothing after a rollback ran `docker compose down`, and nothing if the compose project was
never created. The unit is what makes bring-up idempotent from any state, which is what
"the live URL is reachable after a stop/start cycle" actually requires.

**Files:**
- Create: `infra/deploy/toxic-stack.service`
- Test: `tests/unit/test_systemd_unit.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_systemd_unit.py`:
```python
"""REG-5. Today nothing starts containers on a stop/start cycle."""

import configparser
from pathlib import Path

UNIT = Path("infra/deploy/toxic-stack.service")


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str  # systemd keys are case sensitive
    parser.read_string(UNIT.read_text(encoding="utf-8"))
    return parser


def test_unit_is_enabled_for_multi_user_target():
    assert _unit()["Install"]["WantedBy"] == "multi-user.target"


def test_unit_waits_for_docker_and_the_network():
    unit = _unit()["Unit"]
    assert "docker.service" in unit["After"]
    assert "network-online.target" in unit["After"]
    assert unit["Wants"] == "network-online.target"
    assert unit["Requires"] == "docker.service"


def test_unit_is_a_remain_after_exit_oneshot():
    """compose up -d returns immediately; without RemainAfterExit systemd calls it dead."""
    service = _unit()["Service"]
    assert service["Type"] == "oneshot"
    assert service["RemainAfterExit"] == "yes"


def test_start_brings_the_compose_project_up_and_stop_takes_it_down():
    service = _unit()["Service"]
    assert service["ExecStart"] == (
        "/usr/bin/docker compose --env-file /etc/toxic/stack.env "
        "-f /opt/toxic/compose.yml up -d --remove-orphans"
    )
    assert service["ExecStop"] == (
        "/usr/bin/docker compose --env-file /etc/toxic/stack.env "
        "-f /opt/toxic/compose.yml down"
    )


def test_start_has_a_timeout_large_enough_for_an_ecr_pull():
    assert int(_unit()["Service"]["TimeoutStartSec"]) >= 600


def test_unit_retries_rather_than_giving_up_on_a_cold_boot():
    """RDS can still be starting when EC2 finishes booting. One failure is not terminal."""
    service = _unit()["Service"]
    assert service["Restart"] == "on-failure"
    assert int(service["RestartSec"]) >= 15


def test_unit_carries_no_secret():
    body = UNIT.read_text(encoding="utf-8")
    for forbidden in ("WANDB_API_KEY", "DEMO_API_KEY", "PASSWORD", "AKIA"):
        assert forbidden not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_systemd_unit.py -v`
Expected: FAIL at every test with `FileNotFoundError: infra/deploy/toxic-stack.service`

- [ ] **Step 3: Write minimal implementation**

`infra/deploy/toxic-stack.service`:
```ini
[Unit]
Description=Toxic moderation stack
Documentation=https://github.com/rocklambros/mlops-toxic-moderation/blob/main/infra/ROLLBACK.md
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/toxic
ExecStart=/usr/bin/docker compose --env-file /etc/toxic/stack.env -f /opt/toxic/compose.yml up -d --remove-orphans
ExecStop=/usr/bin/docker compose --env-file /etc/toxic/stack.env -f /opt/toxic/compose.yml down
TimeoutStartSec=900
Restart=on-failure
RestartSec=20

[Install]
WantedBy=multi-user.target
```

`/opt/toxic/compose.yml` is a symlink that user data points at this instance's component
file, so one unit serves all three instances without a template parameter.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_systemd_unit.py -v`
Expected: 7 PASS

Then check it against real systemd syntax on the build box:
```bash
sudo cp infra/deploy/toxic-stack.service /tmp/toxic-stack.service
systemd-analyze verify /tmp/toxic-stack.service && echo "unit is well formed"
```
Expected: `unit is well formed`, with no `Unknown key name` or `Failed to parse` lines. A
warning about `docker.service` not existing on the build box is expected and harmless.

- [ ] **Step 5: Commit**

```bash
git add infra/deploy/toxic-stack.service tests/unit/test_systemd_unit.py
git commit -m "Add the systemd unit that brings the container stack up at boot"
```

---

### Task 5 (H26): User data installs Compose v2 as a checksummed arm64 binary

Amazon Linux 2023 packages Docker Engine. It does **not** package the Compose v2 CLI plugin,
so `dnf install docker-compose-plugin` fails and `dnf install docker-compose` installs the
dead Python v1 if it resolves at all. The plugin has to be fetched as a release binary,
verified, and installed to the system plugin directory. Getting this wrong means `docker
compose` does not exist on a box that has no SSH.

Phase A2 declares the `templatefile()` call and the variables it passes. Phase 5 authors the
script body. If A2 shipped a stub, these tests fail against the stub and the body below
replaces it.

**Files:**
- Create: `infra/terraform/templates/user_data.sh.tftpl`
- Test: `tests/unit/test_user_data.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_user_data.py`:
```python
"""H26. User data runs once, unattended, on a box with no SSH. Every hazard is a test."""

import re
from pathlib import Path

TEMPLATE = Path("infra/terraform/templates/user_data.sh.tftpl")


def _body() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _lines() -> list[str]:
    return [line.strip() for line in _body().splitlines()]


def test_the_script_fails_loudly_rather_than_silently():
    assert _body().splitlines()[0] == "#!/bin/bash"
    assert "set -euxo pipefail" in _body()
    assert "/var/log/user-data.log" in _body(), "the only post-mortem is the console log"


def test_compose_v2_is_not_taken_from_the_distribution_repositories():
    """AL2023 does not package it. dnf-installing it is a boot failure with no SSH."""
    body = _body()
    assert "docker-compose-plugin" not in body
    assert not re.search(r"dnf[^\n]*install[^\n]*docker-compose", body)


def test_user_data_installs_compose_v2_as_a_checksummed_binary():
    body = _body()
    assert "docker-compose-linux-aarch64" in body, "arm64 asset, not x86_64"
    assert re.search(r"COMPOSE_SHA256=[0-9a-f]{64}", body), "pin the checksum, do not fetch it"
    assert "sha256sum -c -" in body


def test_the_checksum_is_verified_before_the_binary_is_installed():
    lines = _lines()
    verify = next(i for i, line in enumerate(lines) if "sha256sum -c -" in line)
    install = next(
        i
        for i, line in enumerate(lines)
        if "install -m 0755" in line and "cli-plugins/docker-compose" in line
    )
    assert verify < install, "the binary is installed before its checksum is checked"


def test_compose_lands_in_the_system_cli_plugin_directory():
    body = _body()
    assert "/usr/libexec/docker/cli-plugins" in body
    assert "docker compose version" in body, "prove the plugin resolved before relying on it"


def test_downloads_retry_because_a_cold_boot_races_the_network():
    for line in _lines():
        if line.startswith("curl "):
            assert "--retry" in line, f"unretried download: {line}"


def test_docker_is_enabled_so_it_survives_a_reboot():
    assert "systemctl enable --now docker" in _body()


def test_the_stack_unit_is_installed_and_enabled():
    body = _body()
    assert "toxic-stack.service" in body
    assert re.search(r"systemctl enable[^\n]*toxic-stack", body)


def test_the_component_compose_file_is_symlinked_to_the_unit_path():
    body = _body()
    assert "ln -sfn" in body
    assert "/opt/toxic/compose.yml" in body
    assert "${component}" in body, "the template must be told which component this host is"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_user_data.py -v`
Expected: FAIL at every test with `FileNotFoundError: infra/terraform/templates/user_data.sh.tftpl`. If Phase A2 shipped a stub, expect `test_compose_v2_is_not_taken_from_the_distribution_repositories` to fail with `AssertionError` instead.

- [ ] **Step 3: Write minimal implementation**

Resolve the real checksum first, so no placeholder digest is ever committed:

```bash
COMPOSE_VERSION=v2.29.7
COMPOSE_SHA256=$(curl -fsSL \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/checksums.txt" \
  | awk '$2 == "docker-compose-linux-aarch64" {print $1}')
test -n "${COMPOSE_SHA256}" || { echo "could not resolve the compose checksum" >&2; exit 1; }
echo "pinning compose ${COMPOSE_VERSION} at ${COMPOSE_SHA256}"
mkdir -p infra/terraform/templates
```

Then write the template, substituting only that value (`$${...}` escapes a shell variable
so Terraform's `templatefile` leaves it alone; `${...}` is a Terraform template variable):

```bash
sed "s/__COMPOSE_SHA256__/${COMPOSE_SHA256}/; s/__COMPOSE_VERSION__/${COMPOSE_VERSION}/" \
  > infra/terraform/templates/user_data.sh.tftpl <<'TFTPL'
#!/bin/bash
# Amazon Linux 2023, arm64. Runs once, unattended, on a host with no SSH and no bastion.
# Terraform variables: ${region} ${component} ${deploy_bucket} ${log_group}
set -euxo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data -s 2>/dev/console) 2>&1

COMPOSE_VERSION="__COMPOSE_VERSION__"
COMPOSE_SHA256="__COMPOSE_SHA256__"

install -d -m 0755 /opt/toxic /etc/toxic /var/lib/toxic/artifacts /var/lib/toxic/spool

# Docker Engine ships in the AL2023 repositories. The Compose v2 CLI plugin does not, so
# it is fetched as a release binary and checked against a pinned digest before it is
# installed. Installing an unverified binary here would be a supply-chain hole on a host
# that holds an instance profile.
dnf -y install docker
systemctl enable --now docker

install -d -m 0755 /usr/libexec/docker/cli-plugins
curl -fsSL --retry 8 --retry-delay 5 --retry-connrefused --max-time 180 \
  -o /tmp/docker-compose \
  "https://github.com/docker/compose/releases/download/$${COMPOSE_VERSION}/docker-compose-linux-aarch64"
printf '%s  /tmp/docker-compose\n' "$${COMPOSE_SHA256}" | sha256sum -c -
install -m 0755 /tmp/docker-compose /usr/libexec/docker/cli-plugins/docker-compose
rm -f /tmp/docker-compose
docker compose version

# The deploy job uploads this SHA's compose files and scripts to S3; the first boot pulls
# whatever is current so a replaced instance is not stranded on an empty /opt/toxic.
aws s3 cp --region ${region} --recursive \
  "s3://${deploy_bucket}/deploy/current/" /opt/toxic/ || true
chmod -R u+rwX,go+rX /opt/toxic

ln -sfn "/opt/toxic/compose.${component}.yml" /opt/toxic/compose.yml

install -m 0644 /opt/toxic/toxic-stack.service /etc/systemd/system/toxic-stack.service
systemctl daemon-reload
systemctl enable toxic-stack.service
TFTPL
```

Two details that are load-bearing. The `dnf -y install docker` line is deliberately not
`docker-compose-plugin`, because that package does not exist in AL2023 and a failed `dnf`
under `set -e` ends user data before anything else runs. The `aws s3 cp ... || true` is
tolerant on first boot because the deploy job may not have run yet; the systemd unit's
`Restart=on-failure` picks it up once it has, and the SSM roll writes the real files.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_user_data.py -v`
Expected: 9 PASS

Then check the rendered script is valid bash:
```bash
sed -e 's/\${region}/us-west-2/g' -e 's/\${component}/backend/g' \
    -e 's/\${deploy_bucket}/example-bucket/g' -e 's/\${log_group}/example-group/g' \
    -e 's/\$\${/${/g' \
    infra/terraform/templates/user_data.sh.tftpl | bash -n && echo "user data parses"
```
Expected: `user data parses`

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/templates/user_data.sh.tftpl tests/unit/test_user_data.py
git commit -m "Bootstrap AL2023 hosts with a checksummed arm64 Compose v2 plugin"
```

---

### Task 6 (H26): User data must not depend on the Elastic IP that attaches after boot

`aws_eip_association` runs after the instance is created. If the subnet does not auto-assign
a public address, the instance boots with **no route to the internet at all**, so `dnf`
hangs, the Compose download fails, and the SSM Agent never reaches `ssm`/`ssmmessages` on
443 — which means the instance never appears in inventory and there is no SSH to fall back
to. Two things close it: the subnet auto-assigns, and user data proves it has a route
before it needs one.

**Files:**
- Modify: `infra/terraform/templates/user_data.sh.tftpl`
- Test: `tests/unit/test_user_data.py` (extend), `tests/infra/test_deploy_tf.py` (create)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_user_data.py`:
```python
def test_user_data_waits_for_an_internet_route_before_it_needs_one():
    """H26. The EIP associates after boot. Without auto-assign there is no route at all."""
    body = _body()
    assert "wait_for_egress" in body, "no bounded connectivity wait exists"
    lines = _lines()
    call = next(i for i, line in enumerate(lines) if line == "wait_for_egress")
    first_dnf = next(i for i, line in enumerate(lines) if line.startswith("dnf "))
    first_curl = next(i for i, line in enumerate(lines) if line.startswith("curl "))
    assert call < first_dnf, "dnf runs before connectivity is proven"
    assert call < first_curl, "curl runs before connectivity is proven"


def test_the_connectivity_wait_is_bounded_and_fails_loudly():
    body = _body()
    assert re.search(r"EGRESS_WAIT_SECONDS=\d+", body), "an unbounded wait is a silent hang"
    assert "no egress route" in body, "the failure must be greppable in the console log"


def test_the_connectivity_probe_uses_443_which_is_the_only_egress_the_group_allows():
    body = _body()
    probe = next(line for line in _lines() if "checkip.amazonaws.com" in line)
    assert probe.startswith("curl "), probe
    assert "https://" in probe, "the security group allows 443, not 80"
```

`tests/infra/test_deploy_tf.py`:
```python
"""Phase 5's Terraform surface, and the one A2 property user data depends on."""

from pathlib import Path

from tests.infra import tfparse

NETWORK = Path("infra/terraform/network.tf")


def test_public_subnets_auto_assign_a_public_ip():
    """H26. If they do not, user data runs before the EIP attaches and has no route."""
    source = NETWORK.read_text(encoding="utf-8")
    subnets = tfparse.resources_of_kind("aws_subnet")
    public = {name: body for name, body in subnets.items() if "public" in name}
    assert public, "no public subnet is declared"
    for name, body in public.items():
        assert body.get("map_public_ip_on_launch") is True, (
            f"{name} does not auto-assign a public IP; user data will boot with no route"
        )
    assert "map_public_ip_on_launch" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_user_data.py tests/infra/test_deploy_tf.py -v`
Expected: `test_user_data_waits_for_an_internet_route_before_it_needs_one` FAILS with `AssertionError: no bounded connectivity wait exists`. `test_public_subnets_auto_assign_a_public_ip` passes if Phase A2 already set it and fails loudly if not.

- [ ] **Step 3: Write minimal implementation**

Insert immediately after the `install -d` line in `infra/terraform/templates/user_data.sh.tftpl`:

```bash
EGRESS_WAIT_SECONDS=300

# The Elastic IP associates AFTER this instance is created. If the subnet does not
# auto-assign a public address there is no route at all, and because there is no SSH, no
# bastion, and no NAT, a silent hang here is an unrecoverable instance. Prove the route
# exists before anything depends on it, and say so in the console log if it does not.
wait_for_egress() {
  local deadline
  deadline=$(( $(date +%s) + EGRESS_WAIT_SECONDS ))
  until curl -fsS --max-time 5 https://checkip.amazonaws.com >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$${deadline}" ]; then
      echo "FATAL: no egress route after $${EGRESS_WAIT_SECONDS}s -- check map_public_ip_on_launch, the route table, and the security group egress rules" >&2
      return 1
    fi
    sleep 5
  done
  echo "egress ok: $(curl -fsS --max-time 5 https://checkip.amazonaws.com)"
}

wait_for_egress
```

If Phase A2 left `map_public_ip_on_launch` unset on the public subnets, set it in
`infra/terraform/network.tf`:

```hcl
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.0.0/24"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = true # user data needs a route BEFORE the EIP associates
  tags                    = { Name = "${var.project}-public-a" }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_user_data.py tests/infra/test_deploy_tf.py -v`
Expected: 12 PASS in `test_user_data.py`, 1 PASS in `test_deploy_tf.py`

Re-check the rendered script parses:
```bash
sed -e 's/\${region}/us-west-2/g' -e 's/\${component}/backend/g' \
    -e 's/\${deploy_bucket}/example-bucket/g' -e 's/\${log_group}/example-group/g' \
    -e 's/\$\${/${/g' \
    infra/terraform/templates/user_data.sh.tftpl | bash -n && echo "user data parses"
```
Expected: `user data parses`

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/templates/user_data.sh.tftpl infra/terraform/network.tf \
        tests/unit/test_user_data.py tests/infra/test_deploy_tf.py
git commit -m "Prove an internet route before user data needs one, and auto-assign on the public subnets"
```

---

### Task 7 (H26): End user data with an externally observable boot marker

A boot that half-succeeded looks exactly like a boot that is still in progress. With no SSH,
the operator needs one question with a yes-or-no answer that can be asked from outside the
instance: *did user data reach the end?* The last line writes an SSM parameter, so both a
human and `aws_up.sh` can ask it.

**Files:**
- Modify: `infra/terraform/templates/user_data.sh.tftpl`, `infra/terraform/iam.tf`
- Create: `infra/terraform/deploy.tf`
- Test: `tests/unit/test_user_data.py` (extend), `tests/infra/test_deploy_tf.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_user_data.py`:
```python
def test_boot_marker_is_the_last_action():
    """H26. A half-finished boot must not look like a slow one."""
    body = _body()
    assert "/toxic/boot/${component}" in body
    meaningful = [line for line in _lines() if line and not line.startswith("#")]
    assert "put-parameter" in meaningful[-1], f"the marker is not last: {meaningful[-1]}"


def test_boot_marker_also_reaches_the_console_log():
    """SSM PutParameter needs a working agent. The console log needs neither."""
    assert "TOXIC-USER-DATA-COMPLETE" in _body()


def test_boot_marker_carries_the_ami_id_so_a_replacement_is_visible():
    body = _body()
    assert "ami-id" in body, "record which AMI booted, so a forced replacement is detectable"
```

Append to `tests/infra/test_deploy_tf.py`:
```python
DEPLOY = Path("infra/terraform/deploy.tf")
IAM = Path("infra/terraform/iam.tf")


def test_instance_roles_may_write_only_the_boot_marker_parameters():
    source = IAM.read_text(encoding="utf-8")
    assert "ssm:PutParameter" in source
    assert "parameter/toxic/boot/*" in source
    assert "parameter/toxic/*" not in source, "do not grant the whole namespace"


def test_deploy_bucket_exists_and_blocks_public_access():
    resources = tfparse.resource_names("aws_s3_bucket")
    assert "deploy" in resources
    assert "deploy" in tfparse.resource_names("aws_s3_bucket_public_access_block")
    assert "deploy" in tfparse.resource_names("aws_s3_bucket_server_side_encryption_configuration")
    assert "deploy" in tfparse.resource_names("aws_s3_bucket_versioning")


def test_endpoint_parameters_are_published_for_the_deploy_job():
    """deploy.yml reads endpoints from SSM so it never has to run terraform output."""
    params = tfparse.resources_of_kind("aws_ssm_parameter")
    names = {body.get("name") for body in params.values()}
    for component in ("backend", "frontend", "monitoring"):
        assert f"/toxic/endpoints/{component}" in names


def test_the_deploy_bucket_is_emptied_on_destroy():
    """terraform destroy is cost control #2; a non-empty bucket blocks it."""
    body = tfparse.resources_of_kind("aws_s3_bucket")["deploy"]
    assert body.get("force_destroy") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_user_data.py tests/infra/test_deploy_tf.py -v`
Expected: `test_boot_marker_is_the_last_action` FAILS with `AssertionError: the marker is not last: systemctl enable toxic-stack.service`; the four `test_deploy_tf.py` additions FAIL with `FileNotFoundError: infra/terraform/deploy.tf`.

- [ ] **Step 3: Write minimal implementation**

Append to `infra/terraform/templates/user_data.sh.tftpl`:

```bash
systemctl start toxic-stack.service || true

# The last line, deliberately. Everything above either succeeded or `set -e` already killed
# this script, so the presence of this parameter is the whole answer to "did the boot
# finish?" -- askable from outside a host that has no SSH.
AMI_ID="$(curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: $(curl -fsS --max-time 5 -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')" http://169.254.169.254/latest/meta-data/ami-id)"
echo "TOXIC-USER-DATA-COMPLETE ${component} $${AMI_ID} $(date -Is)"
aws ssm put-parameter --region ${region} --name "/toxic/boot/${component}" --type String --overwrite --value "ok $(date -Is) $${AMI_ID}"
```

`systemctl start ... || true` is tolerant because the images for this SHA may not be in ECR
yet on a first boot; the unit's `Restart=on-failure` and the SSM roll both converge it. The
marker is unconditional, because it answers "user data finished", not "the app is up" —
`verify_deploy.sh` answers the second question, and conflating them would hide which one
failed.

Add to `infra/terraform/iam.tf`, one statement per instance role:

```hcl
data "aws_iam_policy_document" "boot_marker" {
  statement {
    sid       = "WriteOwnBootMarker"
    effect    = "Allow"
    actions   = ["ssm:PutParameter"]
    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/toxic/boot/*"]
  }
}

resource "aws_iam_role_policy" "boot_marker" {
  for_each = toset(["backend", "frontend", "monitoring"])
  name     = "boot-marker"
  role     = aws_iam_role.instance[each.key].id
  policy   = data.aws_iam_policy_document.boot_marker.json
}
```

`infra/terraform/deploy.tf`:
```hcl
# Phase 5's Terraform surface. Everything here exists so the deploy pipeline never has to
# run `terraform output` inside a world-readable Actions log, and so a rollback can find
# the previous SHA without reading Terraform state.

resource "aws_s3_bucket" "deploy" {
  bucket        = "${var.project}-deploy-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # terraform destroy is cost control #2 and must not be blocked
}

resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id

  rule {
    id     = "expire-old-deploy-payloads"
    status = "Enabled"
    filter {
      prefix = "deploy/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }

  # Database dumps are the graded dashboard dataset. They are NOT expired on a schedule.
  rule {
    id     = "keep-database-dumps"
    status = "Enabled"
    filter {
      prefix = "db/"
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_ssm_parameter" "endpoint" {
  for_each = {
    backend    = "http://${aws_eip.instance["backend"].public_ip}:8000"
    frontend   = "http://${aws_eip.instance["frontend"].public_ip}:8501"
    monitoring = "http://${aws_eip.instance["monitoring"].public_ip}:8502"
  }
  name  = "/toxic/endpoints/${each.key}"
  type  = "String"
  value = each.value
}

output "deploy_bucket" {
  description = "S3 bucket holding per-SHA deploy payloads, the artifact mirror, and DB dumps"
  value       = aws_s3_bucket.deploy.bucket
}
```

The instance roles need read access to the bucket and the deploy role needs write; add both
alongside the boot-marker statement in `iam.tf`, scoped to this bucket ARN.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_user_data.py tests/infra/test_deploy_tf.py -v`
Expected: 15 PASS in `test_user_data.py`, 5 PASS in `test_deploy_tf.py`

Then:
```bash
cd infra/terraform && terraform fmt -check && terraform validate && cd -
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/templates/user_data.sh.tftpl infra/terraform/deploy.tf \
        infra/terraform/iam.tf tests/unit/test_user_data.py tests/infra/test_deploy_tf.py
git commit -m "End user data with an externally observable boot marker and add the deploy bucket"
```

---

### Task 8 (REG-6.3f): `WANDB_API_KEY` reaches a build only through a BuildKit secret mount

A `--build-arg` or an `ENV` writes the key into an image layer permanently, and these images
are pushed to a registry. The serving images do not need the key at all — Phase 2 fetches
the artifact at deploy time — so the only image allowed to see it is the optional
artifact-bake image, and it sees it through a mount that leaves no layer behind.

**Files:**
- Create: `infra/deploy/Dockerfile.artifacts`
- Test: `tests/unit/test_buildkit_secrets.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_buildkit_secrets.py`:
```python
"""Delivery spec section 6.3: the key reaches the build only through a secret mount."""

import re
from pathlib import Path

SERVING_DOCKERFILES = [
    Path("backend/Dockerfile"),
    Path("frontend/Dockerfile"),
    Path("frontend/Dockerfile.reviewer"),
    Path("monitoring/Dockerfile"),
    Path("rescorer/Dockerfile"),
]
ARTIFACTS = Path("infra/deploy/Dockerfile.artifacts")
WORKFLOWS = list(Path(".github/workflows").glob("*.yml"))


def test_no_serving_image_mentions_the_wandb_key_at_all():
    for path in SERVING_DOCKERFILES:
        assert "WANDB" not in path.read_text(encoding="utf-8"), path


def test_no_build_arg_or_env_carries_the_wandb_key():
    for path in [*SERVING_DOCKERFILES, ARTIFACTS]:
        body = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*ARG\s+WANDB", body, re.M), path
        assert not re.search(r"^\s*ENV\s+WANDB", body, re.M), path


def test_the_artifact_image_uses_a_buildkit_secret_mount():
    body = ARTIFACTS.read_text(encoding="utf-8")
    assert body.splitlines()[0].startswith("# syntax=docker/dockerfile:1"), "BuildKit frontend"
    assert "--mount=type=secret,id=wandb_api_key" in body
    assert "/run/secrets/wandb_api_key" in body


def test_the_artifact_image_verifies_the_digest_it_fetched():
    body = ARTIFACTS.read_text(encoding="utf-8")
    assert "sha256sum -c" in body, "an unverified artifact defeats the fail-closed loader"
    assert "MODEL_CARD.md" in body, "the expected digest comes from the committed card"


def test_no_workflow_passes_the_key_as_a_build_argument():
    for path in WORKFLOWS:
        body = path.read_text(encoding="utf-8")
        assert "build-args" not in body or "WANDB" not in body, path
        assert not re.search(r"--build-arg[^\n]*WANDB", body), path


def test_a_workflow_that_needs_the_key_mounts_it_as_a_secret():
    for path in WORKFLOWS:
        body = path.read_text(encoding="utf-8")
        if "WANDB_API_KEY" in body:
            assert "secrets:" in body or "--secret id=wandb_api_key" in body, path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_buildkit_secrets.py -v`
Expected: `test_the_artifact_image_uses_a_buildkit_secret_mount` FAILS with `FileNotFoundError: infra/deploy/Dockerfile.artifacts`

- [ ] **Step 3: Write minimal implementation**

`infra/deploy/Dockerfile.artifacts`:
```dockerfile
# syntax=docker/dockerfile:1.10
# Optional. Bakes the registered model artifact into a tiny image so a bring-up can proceed
# with no network call to Weights & Biases at all, and seeds the S3 mirror that backs the
# deploy-time fetch. It is the ONLY image in this project that ever sees WANDB_API_KEY.
#
# Build:
#   pass show wandb/api-key | tr -d '\n' > /run/user/$(id -u)/wandb_api_key
#   DOCKER_BUILDKIT=1 docker build -f infra/deploy/Dockerfile.artifacts \
#     --secret id=wandb_api_key,src=/run/user/$(id -u)/wandb_api_key \
#     --build-arg ARTIFACT=toxic-clf --build-arg VERSION=v3 -t toxic-artifacts:v3 .
#   shred -u /run/user/$(id -u)/wandb_api_key
#
# The mount exists only for the duration of the RUN. No ARG and no ENV carries the key, so
# nothing lands in a layer and `docker history` shows nothing.
FROM python:3.11-slim-bookworm AS fetch

ARG ARTIFACT=toxic-clf
ARG VERSION=v3
ARG WANDB_ENTITY=rocklambros
ARG WANDB_PROJECT=toxic-moderation

RUN pip install --no-cache-dir wandb==0.18.5

WORKDIR /work
COPY MODEL_CARD.md /work/MODEL_CARD.md

RUN --mount=type=secret,id=wandb_api_key \
    set -eu; \
    WANDB_API_KEY="$(cat /run/secrets/wandb_api_key)"; export WANDB_API_KEY; \
    wandb artifact get \
      "${WANDB_ENTITY}/${WANDB_PROJECT}/${ARTIFACT}:${VERSION}" --root /work/artifacts; \
    unset WANDB_API_KEY; \
    EXPECTED="$(grep -oE '[0-9a-f]{64}' /work/MODEL_CARD.md | head -1)"; \
    test -n "${EXPECTED}"; \
    printf '%s  /work/artifacts/%s.skops\n' "${EXPECTED}" "${ARTIFACT}" | sha256sum -c -

FROM scratch AS artifacts
COPY --from=fetch /work/artifacts /artifacts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_buildkit_secrets.py -v`
Expected: 6 PASS

Then prove the key never reaches a layer:
```bash
pass show wandb/api-key | tr -d '\n' > "/run/user/$(id -u)/wandb_api_key"
DOCKER_BUILDKIT=1 docker build -f infra/deploy/Dockerfile.artifacts \
  --secret id=wandb_api_key,src="/run/user/$(id -u)/wandb_api_key" \
  --target artifacts -t toxic-artifacts:test .
shred -u "/run/user/$(id -u)/wandb_api_key"
docker history --no-trunc toxic-artifacts:test | grep -ci wandb_api_key || echo "0 layers mention the key"
```
Expected: `0 layers mention the key`

- [ ] **Step 5: Commit**

```bash
git add infra/deploy/Dockerfile.artifacts tests/unit/test_buildkit_secrets.py
git commit -m "Fetch the model artifact through a BuildKit secret mount and verify its digest"
```

---

### Task 9 (REG-10d): Deploy-time artifact fetch with a digest-pinned mirror fallback

The loader fails closed on a digest mismatch, which is correct and non-negotiable. It also
means a Weights & Biases outage at bring-up is indistinguishable from a poisoned artifact,
and the demo is down. The fallback is an S3 mirror keyed **by the digest itself**, so the
mirror is not a second trust root: an object that does not hash to the value in the
committed model card cannot be found under the name the fetcher asks for.

**Files:**
- Create: `tests/infra/shellstub.py`, `infra/deploy/instance/fetch_artifacts.sh`
- Test: `tests/infra/test_fetch_artifacts.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/shellstub.py`:
```python
"""A PATH of fake CLIs, so a shell script's control flow can be tested without AWS."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REAL_TOOLS = ("bash", "sh", "date", "sleep", "printf", "cat", "grep", "sed", "awk",
              "sha256sum", "mkdir", "rm", "mv", "cp", "head", "tr", "test", "env", "jq")


def make_stub(bin_dir: Path, name: str, script: str) -> Path:
    """Write an executable stub named `name` into `bin_dir`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def stub_path(bin_dir: Path) -> str:
    """A PATH holding only the stubs plus the coreutils the script legitimately needs."""
    real = bin_dir / "_real"
    real.mkdir(parents=True, exist_ok=True)
    for tool in REAL_TOOLS:
        found = shutil.which(tool)
        if found and not (real / tool).exists():
            (real / tool).symlink_to(found)
    return f"{bin_dir}:{real}"


def run(script: Path, args: list[str], bin_dir: Path, env: dict[str, str] | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {"PATH": stub_path(bin_dir), "HOME": str(bin_dir), "AWS_REGION": "us-west-2"}
    full_env.update(env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        env=full_env,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
```

`tests/infra/test_fetch_artifacts.py`:
```python
"""REG-10d. A registry outage must not turn the fail-closed loader into a demo outage."""

import hashlib
from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

SCRIPT = Path("infra/deploy/instance/fetch_artifacts.sh").resolve()
GOOD = b"pretend skops artifact bytes"
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()
EVIL = b"poisoned artifact bytes"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "artifacts").mkdir()
    card = tmp_path / "MODEL_CARD.md"
    card.write_text(f"digest: `sha256:{GOOD_SHA}`\n", encoding="utf-8")
    return tmp_path


def _wandb_stub(payload: bytes | None) -> str:
    if payload is None:
        return '#!/bin/bash\necho "wandb: 503 Service Unavailable" >&2\nexit 1\n'
    return (
        "#!/bin/bash\n"
        'root=""\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "--root" ]; then root="$2"; fi; shift; done\n'
        'mkdir -p "$root"\n'
        f"printf '%s' {payload.decode()!r} > \"$root/toxic-clf.skops\"\n"
    )


def _aws_stub(payload: bytes | None) -> str:
    if payload is None:
        return (
            "#!/bin/bash\n"
            'echo "An error occurred (404) when calling the GetObject operation" >&2\n'
            "exit 1\n"
        )
    return (
        "#!/bin/bash\n"
        'dest="${@: -1}"\n'
        f"printf '%s' {payload.decode()!r} > \"$dest\"\n"
    )


def _env(workspace: Path) -> dict[str, str]:
    return {
        "MODEL_CARD_PATH": str(workspace / "MODEL_CARD.md"),
        "ARTIFACT_DIR": str(workspace / "artifacts"),
        "ARTIFACT_NAME": "toxic-clf.skops",
        "WANDB_ARTIFACT": "rocklambros/toxic-moderation/toxic-clf:v3",
        "DEPLOY_BUCKET": "example-bucket",
    }


def test_primary_path_installs_the_verified_artifact(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(GOOD))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode == 0, result.stderr
    assert (workspace / "artifacts" / "toxic-clf.skops").read_bytes() == GOOD


def test_mirror_is_used_when_wandb_fails_and_the_digest_still_gates(tmp_path, workspace):
    """The whole point: a registry outage falls back, and the fallback is still verified."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(GOOD))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode == 0, result.stderr
    assert "falling back to the mirror" in result.stdout + result.stderr
    assert (workspace / "artifacts" / "toxic-clf.skops").read_bytes() == GOOD


def test_a_tampered_mirror_object_is_rejected_and_nothing_is_installed(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(EVIL))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert not (workspace / "artifacts" / "toxic-clf.skops").exists()


def test_a_tampered_primary_is_rejected_and_does_not_silently_fall_back(tmp_path, workspace):
    """A digest mismatch is a security event, not a transport failure. Do not retry it."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(EVIL))
    make_stub(bin_dir, "aws", _aws_stub(GOOD))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert "digest mismatch" in (result.stdout + result.stderr).lower()
    assert not (workspace / "artifacts" / "toxic-clf.skops").exists()


def test_both_sources_failing_is_a_hard_failure(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub(None))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0


def test_the_mirror_key_is_the_digest_itself(tmp_path, workspace):
    """The mirror is not a second trust root; the digest IS the lookup key."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert 'artifacts/$${EXPECTED' in body or "artifacts/${EXPECTED" in body


def test_the_expected_digest_comes_from_the_committed_model_card(tmp_path, workspace):
    body = SCRIPT.read_text(encoding="utf-8")
    assert "MODEL_CARD_PATH" in body
    assert "MODEL_DIGEST" not in body.split("# provenance")[0], (
        "the environment must not be able to supply the expected value"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_fetch_artifacts.py -v`
Expected: 7 failures; the first is `test_primary_path_installs_the_verified_artifact` with `bash: infra/deploy/instance/fetch_artifacts.sh: No such file or directory` and `returncode == 127`.

- [ ] **Step 3: Write minimal implementation**

`infra/deploy/instance/fetch_artifacts.sh`:
```bash
#!/usr/bin/env bash
# Deploy-time model artifact fetch. Runs ON the instance, under the instance profile.
#
# provenance: the expected digest is read from the git-committed MODEL_CARD.md and from
# nowhere else. The environment cannot supply it, because "the thing that gave me the
# artifact also told me what it should hash to" is not provenance.
#
# Primary source is the Weights & Biases registry. A registry outage at bring-up would
# otherwise turn the fail-closed loader into a demo outage, so an S3 mirror backs it. The
# mirror key IS the digest, so the mirror is not a second trust root: an object that does
# not hash to the card's value cannot be found under the name this script asks for.
set -euo pipefail

MODEL_CARD_PATH="${MODEL_CARD_PATH:-/opt/toxic/MODEL_CARD.md}"
ARTIFACT_DIR="${ARTIFACT_DIR:-/var/lib/toxic/artifacts}"
ARTIFACT_NAME="${ARTIFACT_NAME:-toxic-clf.skops}"
WANDB_ARTIFACT="${WANDB_ARTIFACT:?WANDB_ARTIFACT must be set}"
DEPLOY_BUCKET="${DEPLOY_BUCKET:?DEPLOY_BUCKET must be set}"
REGION="${AWS_REGION:-us-west-2}"

log() { printf 'fetch_artifacts: %s\n' "$*"; }
die() { printf 'fetch_artifacts: FATAL: %s\n' "$*" >&2; exit 1; }

EXPECTED="$(grep -oE '[0-9a-f]{64}' "${MODEL_CARD_PATH}" | head -1 || true)"
[ -n "${EXPECTED}" ] || die "no 64-hex digest of record in ${MODEL_CARD_PATH}"
log "digest of record ${EXPECTED} (from ${MODEL_CARD_PATH})"

STAGING="$(mktemp -d)"
trap 'rm -rf "${STAGING}"' EXIT
TARGET="${STAGING}/${ARTIFACT_NAME}"

verify() {
  printf '%s  %s\n' "${EXPECTED}" "$1" | sha256sum -c - >/dev/null 2>&1
}

install_verified() {
  mkdir -p "${ARTIFACT_DIR}"
  mv "${TARGET}" "${ARTIFACT_DIR}/${ARTIFACT_NAME}"
  chmod 0444 "${ARTIFACT_DIR}/${ARTIFACT_NAME}"
  log "installed ${ARTIFACT_DIR}/${ARTIFACT_NAME}"
}

# --- Primary: the registry. The key is read from Secrets Manager under the instance
# --- profile at the moment of use, and is never an argument to anything logged.
if WANDB_API_KEY="$(aws secretsmanager get-secret-value --region "${REGION}" \
      --secret-id toxic/wandb-api-key --query SecretString --output text 2>/dev/null)"; then
  export WANDB_API_KEY
fi
if wandb artifact get "${WANDB_ARTIFACT}" --root "${STAGING}" >/dev/null 2>&1; then
  unset WANDB_API_KEY || true
  [ -f "${TARGET}" ] || die "registry returned no ${ARTIFACT_NAME}"
  if verify "${TARGET}"; then
    log "registry copy verified"
    install_verified
    exit 0
  fi
  die "digest mismatch on the registry copy -- refusing to load and refusing to fall back"
fi
unset WANDB_API_KEY || true

# --- Fallback: the digest-keyed mirror.
log "registry fetch failed; falling back to the mirror"
MIRROR_KEY="artifacts/${EXPECTED}/${ARTIFACT_NAME}"
aws s3 cp --region "${REGION}" "s3://${DEPLOY_BUCKET}/${MIRROR_KEY}" "${TARGET}" \
  || die "mirror fetch failed: s3://${DEPLOY_BUCKET}/${MIRROR_KEY}"
verify "${TARGET}" || die "digest mismatch on the mirror copy"
log "mirror copy verified"
install_verified
```

Seed the mirror once, from the artifact-bake image built in Task 8:

```bash
DEPLOY_BUCKET=$(cd infra/terraform && terraform output -raw deploy_bucket)
DIGEST=$(grep -oE '[0-9a-f]{64}' MODEL_CARD.md | head -1)
docker create --name toxic-artifacts-tmp toxic-artifacts:v3
docker cp toxic-artifacts-tmp:/artifacts/toxic-clf.skops /tmp/toxic-clf.skops
docker rm toxic-artifacts-tmp
printf '%s  /tmp/toxic-clf.skops\n' "$DIGEST" | sha256sum -c -
aws s3 cp /tmp/toxic-clf.skops "s3://${DEPLOY_BUCKET}/artifacts/${DIGEST}/toxic-clf.skops"
rm -f /tmp/toxic-clf.skops
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_fetch_artifacts.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/infra/shellstub.py infra/deploy/instance/fetch_artifacts.sh \
        tests/infra/test_fetch_artifacts.py
git commit -m "Fetch the model artifact by digest with a digest-keyed S3 mirror fallback"
```

---

### Task 10 (REG-6.3f): No secret value crosses SendCommand; the instance reads Secrets Manager

The full command text of an SSM `SendCommand` is recorded in CloudTrail and returned by
`aws ssm list-commands` in plaintext to anyone who can read the account. A key passed as a
`--parameters` value is a permanently logged credential. So the payload is one line naming a
script, and the script — running on the instance under its own profile — is what reads the
secret.

**Files:**
- Create: `infra/deploy/instance/bootstrap.sh`, `infra/deploy/instance/roll.sh`
- Test: `tests/infra/test_roll_secrets.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_roll_secrets.py`:
```python
"""Delivery spec section 6.3, deploy side. Nothing secret may travel in a logged argument."""

import re
from pathlib import Path

BOOTSTRAP = Path("infra/deploy/instance/bootstrap.sh")
ROLL = Path("infra/deploy/instance/roll.sh")
SSM_RUN = Path("infra/aws/ssm_run.sh")
WORKFLOW = Path(".github/workflows/deploy.yml")

SECRET_NAMES = (
    "WANDB_API_KEY", "DEMO_API_KEY", "REVIEWER_SHARED_SECRET",
    "SUBMITTER_FP_KEY", "POSTGRES_PASSWORD",
)


def test_the_send_command_payload_is_one_line_naming_a_script():
    """A script body in --parameters is a CloudTrail record of the whole deploy."""
    body = WORKFLOW.read_text(encoding="utf-8")
    invocations = re.findall(r"ssm_run\.sh\s+\S+\s+\S+\s+(.+)", body)
    assert invocations, "deploy.yml does not call ssm_run.sh"
    for command in invocations:
        assert "/opt/toxic/" in command, f"the payload is not a script reference: {command}"
        assert len(command) < 120, f"the payload is a script body, not a reference: {command}"


def test_send_command_payload_contains_no_secret_value():
    body = WORKFLOW.read_text(encoding="utf-8")
    for line in body.splitlines():
        if "ssm_run.sh" not in line:
            continue
        assert "${{ secrets." not in line, f"a GitHub secret is interpolated into SSM: {line}"
        for name in SECRET_NAMES:
            assert f"{name}=" not in line, f"{name} travels as a SendCommand parameter: {line}"


def test_every_secret_is_read_on_the_instance_from_secrets_manager():
    body = ROLL.read_text(encoding="utf-8")
    assert "secretsmanager get-secret-value" in body
    assert "--secret-id toxic/" in body


def test_the_written_env_files_are_not_world_readable():
    body = ROLL.read_text(encoding="utf-8")
    assert re.search(r"(umask 0?077|chmod 0?600)", body), "env files hold live credentials"


def test_secret_values_are_never_echoed():
    for path in (BOOTSTRAP, ROLL):
        body = path.read_text(encoding="utf-8")
        assert "set -x" not in body, f"{path} traces every expansion, including secrets"
        for name in SECRET_NAMES:
            assert not re.search(rf"echo[^\n]*\$\{{?{name}", body), f"{path} echoes {name}"


def test_bootstrap_pins_the_payload_to_the_requested_sha():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'deploy/${SHA}/' in body or "deploy/$1/" in body
    assert "aws s3 cp" in body


def test_bootstrap_refuses_to_run_without_a_sha():
    body = BOOTSTRAP.read_text(encoding="utf-8")
    assert "${1:?" in body


def test_the_database_password_is_never_written_to_a_file_in_plaintext_by_the_operator():
    """RDS manages it in Secrets Manager. roll.sh reads it, nothing else may."""
    body = ROLL.read_text(encoding="utf-8")
    assert "db_master_secret" in body or "toxic/rds" in body or "rds!" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_roll_secrets.py -v`
Expected: FAIL with `FileNotFoundError: infra/deploy/instance/bootstrap.sh`

- [ ] **Step 3: Write minimal implementation**

`infra/deploy/instance/bootstrap.sh`:
```bash
#!/usr/bin/env bash
# What SendCommand actually runs. The whole payload is `bash /opt/toxic/bootstrap.sh <sha>`,
# so the CloudTrail record of a deploy is one readable line with no secret in it.
set -euo pipefail

SHA="${1:?usage: bootstrap.sh <git-sha>}"
REGION="${AWS_REGION:-us-west-2}"
DEPLOY_BUCKET="$(aws ssm get-parameter --region "${REGION}" \
  --name /toxic/deploy/bucket --query 'Parameter.Value' --output text)"

install -d -m 0755 /opt/toxic
aws s3 cp --region "${REGION}" --recursive \
  "s3://${DEPLOY_BUCKET}/deploy/${SHA}/" /opt/toxic/
chmod 0755 /opt/toxic/*.sh

exec bash /opt/toxic/roll.sh "${SHA}"
```

`infra/deploy/instance/roll.sh`:
```bash
#!/usr/bin/env bash
# Runs on the instance, under the instance profile. Reads every credential from Secrets
# Manager at the moment of use, writes them to a 0600 env file that only root can read, and
# restarts the systemd unit. No secret is ever an argument, an echo, or a trace.
#
# Deliberately no `set -x`: it would print every expansion, including the secrets below.
set -euo pipefail
umask 077

SHA="${1:?usage: roll.sh <git-sha>}"
REGION="${AWS_REGION:-us-west-2}"
COMPONENT="$(cat /etc/toxic/component)"

param() {
  aws ssm get-parameter --region "${REGION}" --name "$1" --query 'Parameter.Value' --output text
}
secret() {
  aws secretsmanager get-secret-value --region "${REGION}" \
    --secret-id "$1" --query 'SecretString' --output text
}

REGISTRY="$(param /toxic/deploy/registry)"
LOG_GROUP_BACKEND="$(param /toxic/logs/backend)"
LOG_GROUP_FRONTEND="$(param /toxic/logs/frontend)"
LOG_GROUP_MONITORING="$(param /toxic/logs/monitoring)"
LOG_GROUP_RESCORER="$(param /toxic/logs/rescorer)"

aws ecr get-login-password --region "${REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

# Resolve every tag to an immutable digest here, once, so the compose file never floats a
# tag and a restart six hours from now runs exactly the bytes this deploy verified.
digest_of() {
  aws ecr describe-images --region "${REGION}" --repository-name "toxic-$1" \
    --image-ids "imageTag=$2" --query 'imageDetails[0].imageDigest' --output text
}

{
  printf 'AWS_REGION=%s\n' "${REGION}"
  printf 'LOG_GROUP_BACKEND=%s\n' "${LOG_GROUP_BACKEND}"
  printf 'LOG_GROUP_FRONTEND=%s\n' "${LOG_GROUP_FRONTEND}"
  printf 'LOG_GROUP_MONITORING=%s\n' "${LOG_GROUP_MONITORING}"
  printf 'LOG_GROUP_RESCORER=%s\n' "${LOG_GROUP_RESCORER}"
  printf 'BACKEND_IMAGE=%s/toxic-backend@%s\n'       "${REGISTRY}" "$(digest_of backend "${SHA}")"
  printf 'FRONTEND_IMAGE=%s/toxic-frontend@%s\n'     "${REGISTRY}" "$(digest_of frontend "${SHA}")"
  printf 'REVIEWER_IMAGE=%s/toxic-frontend@%s\n'     "${REGISTRY}" "$(digest_of frontend "${SHA}-reviewer")"
  printf 'MONITORING_IMAGE=%s/toxic-monitoring@%s\n' "${REGISTRY}" "$(digest_of monitoring "${SHA}")"
  printf 'RESCORER_IMAGE=%s/toxic-rescorer@%s\n'     "${REGISTRY}" "$(digest_of rescorer "${SHA}" || echo none)"
} > /etc/toxic/stack.env
chmod 0600 /etc/toxic/stack.env

DB_SECRET_ARN="$(param /toxic/db/master-secret-arn)"
DB_ENDPOINT="$(param /toxic/db/endpoint)"
DB_USER="$(secret "${DB_SECRET_ARN}" | jq -r .username)"
DB_PASS="$(secret "${DB_SECRET_ARN}" | jq -r .password)"

case "${COMPONENT}" in
  backend)
    {
      printf 'DATABASE_URL=postgresql+psycopg://%s:%s@%s/toxicmod\n' "${DB_USER}" "${DB_PASS}" "${DB_ENDPOINT}"
      printf 'DEMO_API_KEY=%s\n' "$(secret toxic/demo-api-key)"
      printf 'REVIEWER_SHARED_SECRET=%s\n' "$(secret toxic/reviewer-shared-secret)"
      printf 'SUBMITTER_FP_KEY=%s\n' "$(secret toxic/submitter-fp-key)"
      printf 'MODEL_ARTIFACT_PATH=/artifacts/toxic-clf.skops\n'
      printf 'MODEL_CARD_PATH=/app/MODEL_CARD.md\n'
      printf 'THRESHOLDS_PATH=/artifacts/thresholds.json\n'
      printf 'SPOOL_PATH=/var/lib/toxic/predictions.spool\n'
    } > /etc/toxic/backend.env
    chmod 0600 /etc/toxic/backend.env
    WANDB_ARTIFACT="$(param /toxic/model/wandb-artifact)" \
      DEPLOY_BUCKET="$(param /toxic/deploy/bucket)" \
      bash /opt/toxic/fetch_artifacts.sh
    ;;
  frontend)
    {
      printf 'BACKEND_URL=%s\n' "$(param /toxic/endpoints/backend)"
      printf 'DEMO_API_KEY=%s\n' "$(secret toxic/demo-api-key)"
      printf 'REVIEWER_SHARED_SECRET=%s\n' "$(secret toxic/reviewer-shared-secret)"
    } > /etc/toxic/frontend.env
    chmod 0600 /etc/toxic/frontend.env
    ;;
  monitoring)
    RO_SECRET_ARN="$(param /toxic/db/readonly-secret-arn)"
    RO_USER="$(secret "${RO_SECRET_ARN}" | jq -r .username)"
    RO_PASS="$(secret "${RO_SECRET_ARN}" | jq -r .password)"
    {
      printf 'MONITORING_DB_DSN=postgresql+psycopg://%s:%s@%s/toxicmod\n' "${RO_USER}" "${RO_PASS}" "${DB_ENDPOINT}"
      printf 'BASELINE_PATH=/artifacts/baseline_flag_rates.json\n'
      printf 'THRESHOLDS_PATH=/artifacts/thresholds.json\n'
    } > /etc/toxic/monitoring.env
    chmod 0600 /etc/toxic/monitoring.env
    ;;
esac
unset DB_PASS RO_PASS 2>/dev/null || true

ln -sfn "/opt/toxic/compose.${COMPONENT}.yml" /opt/toxic/compose.yml
install -m 0644 /opt/toxic/toxic-stack.service /etc/systemd/system/toxic-stack.service
systemctl daemon-reload
systemctl enable toxic-stack.service
systemctl restart toxic-stack.service
docker image prune -f --filter "until=168h" >/dev/null 2>&1 || true
printf 'roll: %s now serving %s\n' "${COMPONENT}" "${SHA}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_roll_secrets.py -v`
Expected: 8 PASS (`test_the_send_command_payload_is_one_line_naming_a_script` and `test_send_command_payload_contains_no_secret_value` will fail until Task 13 writes `deploy.yml`; run them again at the end of Task 13, and mark this task complete only when all eight are green).

Also syntax-check both scripts:
```bash
bash -n infra/deploy/instance/bootstrap.sh infra/deploy/instance/roll.sh && echo "both parse"
```
Expected: `both parse`

- [ ] **Step 5: Commit**

```bash
git add infra/deploy/instance/bootstrap.sh infra/deploy/instance/roll.sh \
        tests/infra/test_roll_secrets.py
git commit -m "Read deploy credentials on the instance instead of passing them through SSM"
```

---

### Task 10a: Every artifact an env file names is an artifact something fetches [gap `DRIFT-ARTIFACTS`]

The drift reference and the decision thresholds are **never delivered to any instance**, and this is a first-boot outage on rubric 3.2 and 2.1 that no test in any plan catches.

`roll.sh` (Task 10) writes `THRESHOLDS_PATH=/artifacts/thresholds.json` into `backend.env`, and `BASELINE_PATH=/artifacts/baseline_flag_rates.json` plus `THRESHOLDS_PATH=/artifacts/thresholds.json` into `monitoring.env`. `compose.monitoring.yml` mounts `/var/lib/toxic/artifacts:/artifacts:ro`. But:

- the `monitoring)` branch of `roll.sh` **never calls** `fetch_artifacts.sh`;
- `fetch_artifacts.sh` (Task 9) is hardcoded to a single file — `ARTIFACT_NAME="${ARTIFACT_NAME:-toxic-clf.skops}"`, `install_verified()` moves only `${TARGET}`, and `trap 'rm -rf "${STAGING}"' EXIT` deletes everything else the registry returned.

Consequence on the deployed stack: `/artifacts` on EC2 #3 is empty, and `monitoring/baseline.py::load_baseline` — which Phase 3 Task 3 deliberately made **fail-closed** with `BaselineMissingError` — kills the dashboard. On EC2 #1 the backend lifespan's `load_thresholds(settings.thresholds_path)` raises and the container never accepts traffic. Every existing threshold and baseline test reads `tests/fixtures/`, so all of them are green.

The two sidecars are also security-relevant in their own right: `thresholds.json` **is** the decision boundary. An unverified thresholds file is a silent policy change, so it gets the same digest-of-record treatment as the coefficients.

**Files:**
- Modify: `infra/deploy/instance/fetch_artifacts.sh`, `infra/deploy/instance/roll.sh`, `MODEL_CARD.md` (extend Phase 2 Task 5's digest block to three digests)
- Test: `tests/infra/test_fetch_artifacts.py` (append), `tests/infra/test_roll_secrets.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_fetch_artifacts.py`:
```python
def test_the_fetcher_installs_thresholds_and_the_drift_baseline(tmp_path, workspace):
    """The dashboard fails closed without baseline_flag_rates.json, and the backend fails
    closed without thresholds.json. Both must land in ARTIFACT_DIR alongside the model."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, THRESHOLDS, BASELINE))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode == 0, result.stderr
    installed = {p.name for p in (workspace / "artifacts").iterdir()}
    assert installed == {"toxic-clf.skops", "thresholds.json", "baseline_flag_rates.json"}


def test_a_missing_sidecar_artifact_fails_the_fetch(tmp_path, workspace):
    """Fail at fetch time on the instance, not at import time inside the container, where
    the only symptom is a restart loop and a log line nobody is watching yet."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, THRESHOLDS, None))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert "baseline_flag_rates.json" in result.stderr


def test_sidecar_artifacts_are_digest_verified_against_the_model_card():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "SIDECAR_DIGESTS" in body or "thresholds_sha256" in body, (
        "the decision boundary is as security-relevant as the coefficients"
    )


def test_a_tampered_sidecar_is_refused_and_does_not_fall_back(tmp_path, workspace):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "wandb", _wandb_stub_multi(GOOD, TAMPERED_THRESHOLDS, BASELINE))
    make_stub(bin_dir, "aws", _aws_stub(None))
    result = run(SCRIPT, [], bin_dir, env=_env(workspace))
    assert result.returncode != 0
    assert "digest mismatch" in result.stderr
    assert not (workspace / "artifacts").exists() or not list((workspace / "artifacts").iterdir())
```

Append to `tests/infra/test_roll_secrets.py`:
```python
def test_the_monitoring_component_fetches_its_artifacts():
    """EC2 #3 mounts /artifacts read-only and reads BASELINE_PATH from it. Nothing
    populated that directory, so the drift panel died on first boot."""
    body = Path("infra/deploy/instance/roll.sh").read_text(encoding="utf-8")
    monitoring_branch = body.split("monitoring)")[1].split(";;")[0]
    assert "fetch_artifacts.sh" in monitoring_branch


def test_every_artifact_path_written_into_an_env_file_is_actually_fetched():
    """The general form of the bug: a path in an env file is a promise, and the only thing
    that keeps it is a fetcher that knows the filename."""
    roll = Path("infra/deploy/instance/roll.sh").read_text(encoding="utf-8")
    fetch = Path("infra/deploy/instance/fetch_artifacts.sh").read_text(encoding="utf-8")
    declared = set(re.findall(r"=(/artifacts/[A-Za-z0-9_.-]+)", roll))
    fetched = set(re.findall(r"/artifacts/([A-Za-z0-9_.-]+)", fetch)) | set(
        re.findall(r'ARTIFACT_NAMES="([^"]+)"', fetch)[0].split()
        if re.findall(r'ARTIFACT_NAMES="([^"]+)"', fetch) else []
    )
    assert {Path(d).name for d in declared} <= fetched, (
        f"env files reference artifacts nothing fetches: {sorted(declared)}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_fetch_artifacts.py tests/infra/test_roll_secrets.py -v`
Expected: FAIL — `assert {'toxic-clf.skops'} == {'toxic-clf.skops', 'thresholds.json', 'baseline_flag_rates.json'}`, and `AssertionError: env files reference artifacts nothing fetches: ['/artifacts/baseline_flag_rates.json', '/artifacts/thresholds.json']`.

- [ ] **Step 3: Write minimal implementation**

1. Extend Phase 2 Task 5's "Artifact digest of record" block in `MODEL_CARD.md` to three rows:
```markdown
## Artifact digest of record

| Artifact | sha256 |
|---|---|
| `toxic-clf.skops` | `<64 hex>` |
| `thresholds.json` | `<64 hex>` |
| `baseline_flag_rates.json` | `<64 hex>` |
```
   Update Phase 2 Task 5's `read_expected_digest(card_path)` to `read_expected_digests(card_path) -> dict[str, str]`, keyed on filename, and keep a one-argument shim for the model so the loader's call site is unchanged. The parser must key on the **filename**, not on position, because `grep -oE '[0-9a-f]{64}' | head -1` silently becomes the wrong digest the moment a row is reordered.

2. Rewrite the single-artifact machinery in `fetch_artifacts.sh` as a loop:
```bash
ARTIFACT_NAMES="${ARTIFACT_NAMES:-toxic-clf.skops thresholds.json baseline_flag_rates.json}"

# name -> expected digest, parsed from the git-committed card by NAME, never by position.
declare -A SIDECAR_DIGESTS
for name in ${ARTIFACT_NAMES}; do
  digest="$(grep -F "\`${name}\`" "${MODEL_CARD_PATH}" | grep -oE '[0-9a-f]{64}' | head -1 || true)"
  [ -n "${digest}" ] || die "no digest of record for ${name} in ${MODEL_CARD_PATH}"
  SIDECAR_DIGESTS["${name}"]="${digest}"
done

verify() { printf '%s  %s\n' "$2" "$1" | sha256sum -c - >/dev/null 2>&1; }

install_verified() { # name
  local name="$1"
  [ -f "${STAGING}/${name}" ] || die "registry returned no ${name}"
  verify "${STAGING}/${name}" "${SIDECAR_DIGESTS[${name}]}" \
    || die "digest mismatch on ${name} -- refusing to install and refusing to fall back"
  mkdir -p "${ARTIFACT_DIR}"
  install -m 0444 "${STAGING}/${name}" "${ARTIFACT_DIR}/${name}"
  log "installed ${ARTIFACT_DIR}/${name}"
}
```
   The primary and mirror paths both iterate `${ARTIFACT_NAMES}`. **Verify every artifact before installing any of them**, so a mismatch on the third file cannot leave the first two installed and the directory half-updated. The mirror key stays digest-derived per file: `artifacts/${SIDECAR_DIGESTS[$name]}/${name}`.

3. Add the fetch to the `monitoring)` branch of `roll.sh`, immediately before the env file is written:
```bash
  monitoring)
    WANDB_ARTIFACT="$(param /toxic/model/wandb-artifact)" \
      DEPLOY_BUCKET="$(param /toxic/deploy/bucket)" \
      ARTIFACT_NAMES="thresholds.json baseline_flag_rates.json" \
      bash /opt/toxic/fetch_artifacts.sh
    ...
```
   EC2 #3 does not need the model itself — it never scores anything — so it fetches the two sidecars only. The backend branch keeps the default three-name list.

4. Seed the mirror with all three files, not one, in Task 9's Step 3 seeding block.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_fetch_artifacts.py tests/infra/test_roll_secrets.py -v && bash -n infra/deploy/instance/fetch_artifacts.sh infra/deploy/instance/roll.sh`
Expected: 11 PASS in `test_fetch_artifacts.py`, 10 in `test_roll_secrets.py`, both scripts parse.

- [ ] **Step 5: Commit**

```bash
git add infra/deploy/instance/fetch_artifacts.sh infra/deploy/instance/roll.sh MODEL_CARD.md \
        backend/model_card.py tests/infra/test_fetch_artifacts.py tests/infra/test_roll_secrets.py
git commit -m "Fetch and digest-verify thresholds and the drift baseline on every instance that reads them"
```

---

### Task 11 (H5): Prove the SSM roll actually ran

`aws ssm send-command` is fire-and-forget. A `--targets` expression that matches **zero**
instances still returns a `CommandId` and exits 0, so a deploy job built on `send-command`
alone reports success while nothing was deployed. Three assertions close it, and all three
are tested against a stubbed AWS CLI so the failure modes are exercised without spending a
cent.

**Files:**
- Create: `infra/aws/ssm_run.sh`
- Test: `tests/infra/test_ssm_run.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_ssm_run.py`:
```python
"""H5. The single most dangerous property of the deploy path: green while doing nothing."""

from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

SCRIPT = Path("infra/aws/ssm_run.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
"""A fake `aws` whose behaviour is driven entirely by STUB_* environment variables."""
import os
import sys

argv = sys.argv[1:]


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


count = int(os.environ.get("STUB_INVOCATION_COUNT", "0"))

if "send-command" in argv:
    if os.environ.get("STUB_SEND_FAILS") == "1":
        print("An error occurred (AccessDeniedException)", file=sys.stderr)
        sys.exit(255)
    print("cmd-0001")
    sys.exit(0)

if "list-command-invocations" in argv:
    query = opt("--query", "")
    if query.startswith("length("):
        print(count)
    else:
        print("\t".join(f"i-{n:03d}" for n in range(count)))
    sys.exit(0)

if "get-command-invocation" in argv:
    query = opt("--query")
    instance = opt("--instance-id", "")
    key = instance.replace("-", "_")
    if query == "Status":
        print(os.environ.get(f"STUB_STATUS_{key}", "Success"))
    elif query == "StandardErrorContent":
        print(os.environ.get("STUB_STDERR", ""))
    else:
        print(os.environ.get("STUB_STDOUT", ""))
    sys.exit(0)

print(f"unexpected aws call: {argv}", file=sys.stderr)
sys.exit(9)
'''

FAST = {"SSM_REGISTER_TIMEOUT": "1", "SSM_RUN_TIMEOUT": "2", "SSM_POLL_SECONDS": "0"}


@pytest.fixture()
def bin_dir(tmp_path: Path) -> Path:
    target = tmp_path / "bin"
    make_stub(target, "aws", AWS_STUB)
    return target


def test_zero_matching_instances_fails_the_deploy(bin_dir):
    """The exact H5 failure: a CommandId, exit 0, and nothing deployed."""
    result = run(SCRIPT, ["backend", "1", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "0"})
    assert result.returncode != 0
    assert "saw 0" in result.stderr
    assert "nothing was deployed" in result.stderr


def test_a_partial_fleet_match_fails_the_deploy(bin_dir):
    result = run(SCRIPT, ["backend", "3", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "2"})
    assert result.returncode != 0
    assert "expected 3" in result.stderr


def test_more_instances_than_expected_also_fails(bin_dir):
    """A stray instance carrying the tag means the fleet does not match the plan."""
    result = run(SCRIPT, ["backend", "1", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "2"})
    assert result.returncode != 0


def test_failed_invocation_prints_standard_error_and_fails(bin_dir):
    result = run(SCRIPT, ["backend", "1", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "1", "STUB_STATUS_i_000": "Failed",
                      "STUB_STDERR": "denied: ecr pull permission"})
    assert result.returncode != 0
    assert "denied: ecr pull permission" in result.stderr
    assert "Failed" in result.stdout + result.stderr


def test_a_timed_out_invocation_fails(bin_dir):
    result = run(SCRIPT, ["backend", "1", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "1", "STUB_STATUS_i_000": "TimedOut"})
    assert result.returncode != 0


def test_an_invocation_stuck_in_progress_fails_rather_than_hanging(bin_dir):
    result = run(SCRIPT, ["backend", "1", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "1", "STUB_STATUS_i_000": "InProgress"})
    assert result.returncode != 0
    assert "PollTimeout" in result.stdout + result.stderr


def test_one_failure_among_several_still_fails_the_whole_roll(bin_dir):
    result = run(SCRIPT, ["backend", "3", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "3", "STUB_STATUS_i_001": "Failed"})
    assert result.returncode != 0


def test_all_success_exits_zero(bin_dir):
    result = run(SCRIPT, ["backend", "3", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_INVOCATION_COUNT": "3"})
    assert result.returncode == 0, result.stderr
    assert "matched 3/3" in result.stdout


def test_a_send_command_failure_is_not_swallowed(bin_dir):
    result = run(SCRIPT, ["backend", "1", "bash /opt/toxic/bootstrap.sh abc"], bin_dir,
                 env={**FAST, "STUB_SEND_FAILS": "1"})
    assert result.returncode != 0


def test_missing_arguments_are_a_usage_error_not_a_deploy(bin_dir):
    result = run(SCRIPT, ["backend"], bin_dir, env=FAST)
    assert result.returncode == 2
    assert "usage" in result.stderr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_ssm_run.py -v`
Expected: 10 failures; `test_zero_matching_instances_fails_the_deploy` reports `returncode == 127` and `bash: infra/aws/ssm_run.sh: No such file or directory`.

- [ ] **Step 3: Write minimal implementation**

`infra/aws/ssm_run.sh`:
```bash
#!/usr/bin/env bash
# Run a command on every instance carrying a tag, and PROVE it ran.
#
# `aws ssm send-command` is fire-and-forget. A --targets expression matching ZERO instances
# still returns a CommandId and exits 0, so a deploy job built on send-command alone goes
# green while nothing was deployed. Everything below exists to make that impossible:
#
#   1. the number of invocations must equal the number of instances we expected
#   2. every invocation must reach a terminal state before this returns
#   3. every terminal state must be Success; anything else prints StandardErrorContent
#
# It still does not prove the application works. `verify_deploy.sh` is that gate.
#
# usage: ssm_run.sh <component-tag-value> <expected-count> <remote command...>
set -euo pipefail

usage() { printf 'usage: ssm_run.sh <component> <expected_count> <command...>\n' >&2; exit 2; }

COMPONENT="${1:-}"
EXPECTED="${2:-}"
[ -n "${COMPONENT}" ] && [ -n "${EXPECTED}" ] || usage
shift 2
[ "$#" -gt 0 ] || usage
REMOTE_COMMAND="$*"

REGION="${AWS_REGION:?AWS_REGION must be set}"
TAG_KEY="${SSM_TARGET_TAG:-Component}"
REGISTER_TIMEOUT="${SSM_REGISTER_TIMEOUT:-120}"
RUN_TIMEOUT="${SSM_RUN_TIMEOUT:-900}"
POLL="${SSM_POLL_SECONDS:-5}"

die() { printf 'ssm_run: FATAL: %s\n' "$*" >&2; exit 1; }

command_id="$(aws ssm send-command \
  --region "${REGION}" \
  --document-name AWS-RunShellScript \
  --targets "Key=tag:${TAG_KEY},Values=${COMPONENT}" \
  --parameters "commands=[\"${REMOTE_COMMAND}\"]" \
  --timeout-seconds 600 \
  --comment "toxic roll ${COMPONENT}" \
  --query 'Command.CommandId' --output text)"
[ -n "${command_id}" ] && [ "${command_id}" != "None" ] || die "send-command returned no CommandId"
printf 'ssm_run: %s CommandId=%s\n' "${COMPONENT}" "${command_id}"

# --- Assertion 1: the target expression matched exactly EXPECTED instances. ---
deadline=$(( $(date +%s) + REGISTER_TIMEOUT ))
observed=0
while :; do
  observed="$(aws ssm list-command-invocations --region "${REGION}" \
      --command-id "${command_id}" --query 'length(CommandInvocations)' --output text)"
  [ "${observed}" = "None" ] && observed=0
  [ "${observed}" -ge "${EXPECTED}" ] && break
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    die "expected ${EXPECTED} invocations for tag ${TAG_KEY}=${COMPONENT}, saw ${observed} after ${REGISTER_TIMEOUT}s -- nothing was deployed"
  fi
  sleep "${POLL}"
done
[ "${observed}" -eq "${EXPECTED}" ] || \
  die "expected ${EXPECTED} invocations for tag ${TAG_KEY}=${COMPONENT}, saw ${observed} -- the running fleet does not match the plan"
printf 'ssm_run: %s matched %s/%s instances\n' "${COMPONENT}" "${observed}" "${EXPECTED}"

instance_ids="$(aws ssm list-command-invocations --region "${REGION}" \
    --command-id "${command_id}" --query 'CommandInvocations[].InstanceId' --output text)"

# --- Assertions 2 and 3: poll each invocation to a terminal state; only Success passes. ---
failed=0
for instance in ${instance_ids}; do
  run_deadline=$(( $(date +%s) + RUN_TIMEOUT ))
  status="Pending"
  while :; do
    status="$(aws ssm get-command-invocation --region "${REGION}" \
        --command-id "${command_id}" --instance-id "${instance}" \
        --query 'Status' --output text)"
    case "${status}" in
      Success|Failed|Cancelled|TimedOut) break ;;
    esac
    if [ "$(date +%s)" -ge "${run_deadline}" ]; then
      status="PollTimeout"
      break
    fi
    sleep "${POLL}"
  done
  printf 'ssm_run: %s %s -> %s\n' "${COMPONENT}" "${instance}" "${status}"
  if [ "${status}" != "Success" ]; then
    failed=1
    printf '--- %s StandardErrorContent ---\n' "${instance}" >&2
    aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" \
      --instance-id "${instance}" --query 'StandardErrorContent' --output text >&2 || true
    printf '--- %s StandardOutputContent ---\n' "${instance}" >&2
    aws ssm get-command-invocation --region "${REGION}" --command-id "${command_id}" \
      --instance-id "${instance}" --query 'StandardOutputContent' --output text >&2 || true
  fi
done
[ "${failed}" -eq 0 ] || die "${COMPONENT}: at least one invocation did not reach Success"
printf 'ssm_run: %s OK\n' "${COMPONENT}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_ssm_run.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add infra/aws/ssm_run.sh tests/infra/test_ssm_run.py
git commit -m "Assert the SSM roll reached every instance and succeeded on each"
```

---

### Task 12 (H5, H26): `curl /health` against three Elastic IPs is the real gate

An SSM invocation reporting `Success` means a shell exited 0 on a box. It does not mean the
container started, the artifact verified, RDS was reachable, or the security group lets the
grader in. This script makes the statement that is actually worth making, and it is what
fails the deploy job.

**Files:**
- Create: `infra/aws/verify_deploy.sh`
- Test: `tests/infra/test_verify_deploy.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_verify_deploy.py`:
```python
"""H5. SSM Success is not a deploy. Three endpoints answering is a deploy."""

import http.server
import json
import threading
from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

SCRIPT = Path("infra/aws/verify_deploy.sh").resolve()
FAST = {"CURL_RETRY": "0", "CURL_RETRY_DELAY": "0", "CURL_MAX_TIME": "3"}

BACKEND_OK = json.dumps(
    {"status": "ok", "model_version": "toxic-clf:v3", "database": "ok", "spool_depth": 0}
)
BACKEND_LEAKY = json.dumps(
    {"status": "ok", "model_version": "toxic-clf:v3@sha256:" + "a" * 64, "database": "ok"}
)


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, tuple[int, str]] = {}

    def do_GET(self):  # noqa: N802
        status, body = self.routes.get(self.path, (404, "not found"))
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the test log
        return


def _serve(routes: dict[str, tuple[int, str]]) -> tuple[int, http.server.HTTPServer]:
    handler = type("H", (_Handler,), {"routes": routes})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1], server


@pytest.fixture()
def stack():
    servers = []

    def start(backend_body=BACKEND_OK, backend_status=200, ui_status=200):
        ports = {}
        for name, routes in (
            ("backend", {"/health": (backend_status, backend_body)}),
            ("frontend", {"/_stcore/health": (ui_status, "ok")}),
            ("monitoring", {"/_stcore/health": (ui_status, "ok")}),
        ):
            port, server = _serve(routes)
            servers.append(server)
            ports[name] = port
        return {
            "BACKEND_URL": f"http://127.0.0.1:{ports['backend']}",
            "FRONTEND_URL": f"http://127.0.0.1:{ports['frontend']}",
            "MONITORING_URL": f"http://127.0.0.1:{ports['monitoring']}",
        }

    yield start
    for server in servers:
        server.shutdown()


def test_all_three_healthy_exits_zero(tmp_path, stack):
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **stack()})
    assert result.returncode == 0, result.stderr
    for name in ("backend", "frontend", "monitoring"):
        assert name in result.stdout


def test_verify_fails_when_one_endpoint_is_down(tmp_path, stack):
    urls = stack()
    urls["MONITORING_URL"] = "http://127.0.0.1:1"
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **urls})
    assert result.returncode != 0
    assert "monitoring" in result.stderr
    assert "DOWN" in result.stderr


def test_verify_fails_when_the_backend_returns_a_bad_status(tmp_path, stack):
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **stack(backend_status=503)})
    assert result.returncode != 0


def test_verify_fails_when_health_leaks_the_artifact_digest(tmp_path, stack):
    """H14. /health goes out of its way to strip the digest; the gate confirms it worked."""
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **stack(backend_body=BACKEND_LEAKY)})
    assert result.returncode != 0
    assert "LEAK" in result.stderr


def test_verify_fails_when_the_backend_reports_the_database_unreachable(tmp_path, stack):
    body = json.dumps({"status": "degraded", "model_version": "toxic-clf:v3", "database": "down"})
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **stack(backend_body=body)})
    assert result.returncode != 0


def test_verify_requires_every_url_to_be_supplied(tmp_path, stack):
    urls = stack()
    del urls["FRONTEND_URL"]
    result = run(SCRIPT, [], tmp_path / "bin", env={**FAST, **urls})
    assert result.returncode != 0
    assert "FRONTEND_URL" in result.stderr


def test_the_streamlit_probes_use_the_stcore_health_path(tmp_path):
    """Streamlit has no /health. Probing / would 200 on a crashed app that still serves HTML."""
    assert "_stcore/health" in SCRIPT.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_verify_deploy.py -v`
Expected: 7 failures, each with `returncode == 127` and `bash: infra/aws/verify_deploy.sh: No such file or directory`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/verify_deploy.sh`:
```bash
#!/usr/bin/env bash
# The REAL deploy gate.
#
# ssm_run.sh proves a shell exited 0 on three boxes. This proves the application answers on
# three Elastic IPs, from outside the VPC, over the same path a grader would take. If this
# fails the deploy failed, whatever SSM said.
set -uo pipefail

BACKEND_URL="${BACKEND_URL:?BACKEND_URL must be set}"
FRONTEND_URL="${FRONTEND_URL:?FRONTEND_URL must be set}"
MONITORING_URL="${MONITORING_URL:?MONITORING_URL must be set}"

RETRY="${CURL_RETRY:-18}"
RETRY_DELAY="${CURL_RETRY_DELAY:-5}"
MAX_TIME="${CURL_MAX_TIME:-10}"

fail=0

check() {
  local name="$1" url="$2" needle="$3" body
  if ! body="$(curl -fsS --max-time "${MAX_TIME}" --retry "${RETRY}" \
        --retry-delay "${RETRY_DELAY}" --retry-all-errors "${url}" 2>/dev/null)"; then
    printf 'verify: %-11s DOWN  %s\n' "${name}" "${url}" >&2
    fail=1
    return
  fi
  if ! printf '%s' "${body}" | grep -qF "${needle}"; then
    printf 'verify: %-11s BAD   %s (missing %s)\n' "${name}" "${url}" "${needle}" >&2
    fail=1
    return
  fi
  # H14: the digest is stripped from the public listener on purpose. Confirm it stayed off.
  if printf '%s' "${body}" | grep -Eq '[0-9a-f]{64}'; then
    printf 'verify: %-11s LEAK  %s exposes a 64-hex artifact digest\n' "${name}" "${url}" >&2
    fail=1
    return
  fi
  printf 'verify: %-11s OK    %s\n' "${name}" "${url}"
}

# The backend must report BOTH itself and its database healthy: rubric 2.2 makes complete
# prediction logging a requirement, so a backend that serves without persisting is not
# a successful deploy.
check backend    "${BACKEND_URL}/health"            '"database": "ok"'
check frontend   "${FRONTEND_URL}/_stcore/health"   'ok'
check monitoring "${MONITORING_URL}/_stcore/health" 'ok'

if [ "${fail}" -ne 0 ]; then
  printf 'verify: DEPLOY GATE FAILED -- see docs/runbooks/no-ssh-debug.md\n' >&2
  exit 1
fi
printf 'verify: all three endpoints healthy\n'
```

The backend needle is `"database": "ok"` rather than `"status": "ok"` because Phase 2's
`/health` reports the two independently, and a backend serving predictions that never reach
Postgres would punch holes in the graded drift and live-accuracy views without ever failing
a naive readiness probe.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_verify_deploy.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add infra/aws/verify_deploy.sh tests/infra/test_verify_deploy.py
git commit -m "Gate the deploy on three live health endpoints rather than on SSM status"
```

---

### Task 13 (H35, C7, DELIV-3): `deploy.yml` — pinned, least-privilege, and apply-free

Four defects close here. Unpinned third-party Actions can mint the `gha-deploy` OIDC token.
The repository's default workflow permission is *write*, and no job narrows it. Unattended
`terraform apply` on every push to `main` means a README typo can replace three instances.
And the account id lands in world-readable Actions logs the moment an ECR URI is printed.

**Files:**
- Create: `.github/workflows/deploy.yml`
- Test: `tests/unit/test_deploy_workflow.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_deploy_workflow.py`:
```python
"""H35, C7, DELIV-3. The deploy workflow is the highest-privilege code in the repository."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(".github/workflows/deploy.yml")


def _doc() -> dict:
    # PyYAML parses the bare key `on:` as boolean True. Normalise it.
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _steps() -> list[dict]:
    steps: list[dict] = []
    for job in _doc()["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def test_every_action_is_pinned_to_a_full_commit_sha():
    """H35. A floating tag on any of these can mint the gha-deploy OIDC token."""
    for step in _steps():
        uses = step.get("uses")
        if not uses:
            continue
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses), f"unpinned action: {uses}"


def test_every_pinned_action_records_the_version_it_pins():
    body = WORKFLOW.read_text(encoding="utf-8")
    for line in body.splitlines():
        if "uses:" in line and "@" in line:
            assert re.search(r"#\s*v?\d+\.\d+", line), f"no version comment: {line.strip()}"


def test_top_level_permissions_are_empty():
    """H35. The repository default is write; an empty top level makes every job opt in."""
    assert _doc()["permissions"] == {}


def test_each_job_requests_only_what_it_needs():
    for name, job in _doc()["jobs"].items():
        permissions = job.get("permissions")
        assert permissions is not None, f"job {name} inherits permissions"
        assert set(permissions) <= {"id-token", "contents", "packages"}, name
        assert permissions.get("contents", "read") == "read", f"job {name} may write the repo"


def test_deploy_workflow_never_runs_terraform_apply():
    """C7. A README typo must not be able to replace all three instances."""
    body = WORKFLOW.read_text(encoding="utf-8")
    assert "terraform apply" not in body
    assert "terraform destroy" not in body


def test_docs_only_pushes_cannot_trigger_deploy():
    paths_ignore = _doc()["on"]["push"]["paths-ignore"]
    for pattern in ("docs/**", "**.md"):
        assert pattern in paths_ignore, f"{pattern} is not ignored"


def test_the_production_environment_gates_the_deploy():
    jobs = _doc()["jobs"]
    assert any(job.get("environment") == "production" for job in jobs.values())


def test_account_id_is_masked_before_the_first_ecr_step():
    """DELIV-3. Actions logs on a public repository are world-readable."""
    steps = _steps()
    mask_index = next(i for i, s in enumerate(steps) if "add-mask" in str(s.get("run", "")))
    ecr_index = next(
        i for i, s in enumerate(steps)
        if "ecr" in str(s.get("uses", "")).lower() or "ecr" in str(s.get("run", "")).lower()
    )
    assert mask_index < ecr_index, "the account id is printed before it is masked"


def test_the_roll_is_asserted_and_then_verified_over_http():
    """H5. The order matters: assert the roll, THEN prove the endpoints answer."""
    body = WORKFLOW.read_text(encoding="utf-8")
    roll = body.index("ssm_run.sh")
    verify = body.index("verify_deploy.sh")
    record = body.index("record_deploy.sh")
    assert roll < verify < record, "a failed deploy must not be recorded as the current SHA"


def test_the_roll_expects_exactly_one_instance_per_component():
    body = WORKFLOW.read_text(encoding="utf-8")
    for component in ("backend", "frontend", "monitoring"):
        assert re.search(rf"ssm_run\.sh {component} 1 ", body), component


def test_the_build_matrix_covers_five_images_across_four_repositories():
    body = WORKFLOW.read_text(encoding="utf-8")
    for image in ("backend", "frontend", "reviewer", "monitoring", "rescorer"):
        assert image in body, image
    assert "-reviewer" in body, "the reviewer image shares the frontend repository by tag"


def test_no_step_writes_a_secret_to_the_log_or_to_ssm():
    body = WORKFLOW.read_text(encoding="utf-8")
    for line in body.splitlines():
        if "ssm" in line and "${{ secrets." in line:
            raise AssertionError(f"a GitHub secret reaches SSM: {line.strip()}")


def test_the_workflow_can_be_dispatched_manually_for_a_rollback():
    assert "workflow_dispatch" in _doc()["on"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_deploy_workflow.py -v`
Expected: FAIL at collection of the first test with `FileNotFoundError: .github/workflows/deploy.yml` (or, if Phase A2 left a skeleton, `test_every_action_is_pinned_to_a_full_commit_sha` with `AssertionError: unpinned action: actions/checkout@v4`).

- [ ] **Step 3: Write minimal implementation**

Resolve the action pins first, so no floating tag is ever committed:

```bash
pin() { gh api "repos/$1/commits/$2" --jq '.sha'; }
echo "checkout      $(pin actions/checkout v4.2.2)"
echo "aws-creds     $(pin aws-actions/configure-aws-credentials v4.0.2)"
echo "ecr-login     $(pin aws-actions/amazon-ecr-login v2.0.1)"
echo "buildx        $(pin docker/setup-buildx-action v3.7.1)"
echo "build-push    $(pin docker/build-push-action v6.10.0)"
```

Then write `.github/workflows/deploy.yml`, substituting those five values for the
`<...-sha>` placeholders below and keeping the trailing version comments:

```yaml
name: deploy

# C7: infrastructure changes never run unattended. `terraform apply` lives in its own
# manually-dispatched workflow, so a documentation commit cannot replace three instances
# mid-grading. paths-ignore stops docs pushes from starting a deploy at all.
on:
  push:
    branches: [main]
    paths-ignore:
      - 'docs/**'
      - '**.md'
      - '.github/ISSUE_TEMPLATE/**'
      - 'LICENSE'
  workflow_dispatch:
    inputs:
      sha:
        description: 'Git SHA to deploy (defaults to the pushed commit)'
        required: false

# H35: the repository default workflow permission is write. An empty map here forces every
# job to opt in to exactly what it needs.
permissions: {}

concurrency:
  group: deploy-production
  cancel-in-progress: false

env:
  AWS_REGION: us-west-2
  IMAGE_TAG: ${{ github.event.inputs.sha || github.sha }}

jobs:
  build:
    name: Build and push arm64 images
    runs-on: ubuntu-24.04-arm
    permissions:
      id-token: write
      contents: read
    strategy:
      fail-fast: true
      matrix:
        include:
          - name: backend
            repository: toxic-backend
            dockerfile: backend/Dockerfile
            suffix: ''
          - name: frontend
            repository: toxic-frontend
            dockerfile: frontend/Dockerfile
            suffix: ''
          - name: reviewer
            repository: toxic-frontend
            dockerfile: frontend/Dockerfile.reviewer
            suffix: '-reviewer'
          - name: monitoring
            repository: toxic-monitoring
            dockerfile: monitoring/Dockerfile
            suffix: ''
          - name: rescorer
            repository: toxic-rescorer
            dockerfile: rescorer/Dockerfile
            suffix: ''
    steps:
      - uses: actions/checkout@<checkout-sha> # v4.2.2

      - uses: aws-actions/configure-aws-credentials@<aws-creds-sha> # v4.0.2
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      # DELIV-3: this repository is public, so these logs are too. Mask the account id
      # before anything can print an ECR URI or a role ARN.
      - name: Mask the account id
        run: echo "::add-mask::$(aws sts get-caller-identity --query Account --output text)"

      - id: ecr
        uses: aws-actions/amazon-ecr-login@<ecr-login-sha> # v2.0.1

      - uses: docker/setup-buildx-action@<buildx-sha> # v3.7.1

      - uses: docker/build-push-action@<build-push-sha> # v6.10.0
        with:
          context: .
          file: ${{ matrix.dockerfile }}
          platforms: linux/arm64
          push: true
          provenance: false
          tags: ${{ steps.ecr.outputs.registry }}/${{ matrix.repository }}:${{ env.IMAGE_TAG }}${{ matrix.suffix }}

  roll:
    name: Roll containers and verify
    needs: build
    runs-on: ubuntu-24.04-arm
    environment: production
    permissions:
      id-token: write
      contents: read
    steps:
      - uses: actions/checkout@<checkout-sha> # v4.2.2

      - uses: aws-actions/configure-aws-credentials@<aws-creds-sha> # v4.0.2
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Mask the account id
        run: echo "::add-mask::$(aws sts get-caller-identity --query Account --output text)"

      - name: Publish this SHA's deploy payload
        run: |
          set -euo pipefail
          BUCKET="$(aws ssm get-parameter --name /toxic/deploy/bucket \
            --query 'Parameter.Value' --output text)"
          STAGE="$(mktemp -d)"
          cp infra/deploy/instance/*.sh "$STAGE/"
          cp infra/deploy/compose.*.yml "$STAGE/"
          cp infra/deploy/toxic-stack.service "$STAGE/"
          cp MODEL_CARD.md "$STAGE/"
          aws s3 cp --recursive "$STAGE" "s3://${BUCKET}/deploy/${IMAGE_TAG}/"
          aws s3 cp --recursive "$STAGE" "s3://${BUCKET}/deploy/current/"

      # H5: send-command is fire-and-forget. ssm_run.sh asserts the invocation count,
      # polls every invocation to a terminal state, and fails on anything but Success.
      - name: Roll each component
        run: |
          set -euo pipefail
          chmod +x infra/aws/*.sh
          infra/aws/ssm_run.sh backend 1 "bash /opt/toxic/bootstrap.sh ${IMAGE_TAG}"
          infra/aws/ssm_run.sh frontend 1 "bash /opt/toxic/bootstrap.sh ${IMAGE_TAG}"
          infra/aws/ssm_run.sh monitoring 1 "bash /opt/toxic/bootstrap.sh ${IMAGE_TAG}"

      # The real gate. SSM Success is not a deploy; three answering endpoints are.
      - name: Verify the live endpoints
        run: |
          set -euo pipefail
          get() { aws ssm get-parameter --name "$1" --query 'Parameter.Value' --output text; }
          BACKEND_URL="$(get /toxic/endpoints/backend)" \
          FRONTEND_URL="$(get /toxic/endpoints/frontend)" \
          MONITORING_URL="$(get /toxic/endpoints/monitoring)" \
            infra/aws/verify_deploy.sh

      # C8: only a verified deploy becomes the rollback baseline.
      - name: Record the deployed SHA
        run: infra/aws/record_deploy.sh "${IMAGE_TAG}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_deploy_workflow.py tests/infra/test_roll_secrets.py tests/unit/test_buildkit_secrets.py -v`
Expected: 13 PASS in `test_deploy_workflow.py`, 8 PASS in `test_roll_secrets.py`, 6 PASS in `test_buildkit_secrets.py`

Then validate the YAML the way GitHub will:
```bash
.venv/bin/python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/deploy.yml')); print('workflow parses')"
gh workflow list --all | grep -F deploy || echo "not registered until the branch merges (expected)"
```
Expected: `workflow parses`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/deploy.yml tests/unit/test_deploy_workflow.py
git commit -m "Add the SHA-pinned least-privilege deploy workflow with an HTTP gate"
```

---

### Task 14 (C8): Record the deployed SHA in SSM Parameter Store

Rollback needs to know what to roll back *to*, and it needs to know it without reading
Terraform state, without a GitHub API call, and without a human remembering. Two parameters
do it, and the order they are written in is the whole trick: write the previous value first,
because a crash between the two writes must lose the new pointer rather than the old one.

**Files:**
- Create: `infra/aws/record_deploy.sh`
- Test: `tests/infra/test_record_deploy.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_record_deploy.py`:
```python
"""C8. The rollback target has to survive the failure that makes rollback necessary."""

from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

SCRIPT = Path("infra/aws/record_deploy.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
journal = Path(os.environ["STUB_JOURNAL"])
store = Path(os.environ["STUB_STORE"])


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


if "get-parameter" in argv:
    name = opt("--name")
    values = dict(
        line.split("=", 1) for line in store.read_text().splitlines() if line
    ) if store.exists() else {}
    if name not in values:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(values[name])
    sys.exit(0)

if "put-parameter" in argv:
    name, value = opt("--name"), opt("--value")
    if os.environ.get("STUB_FAIL_ON") == name:
        print("simulated failure", file=sys.stderr)
        sys.exit(255)
    with journal.open("a") as handle:
        handle.write(f"{name}={value}\n")
    values = dict(
        line.split("=", 1) for line in store.read_text().splitlines() if line
    ) if store.exists() else {}
    values[name] = value
    store.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    sys.exit(0)

print(f"unexpected: {argv}", file=sys.stderr)
sys.exit(9)
'''


@pytest.fixture()
def env(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    journal = tmp_path / "journal"
    store = tmp_path / "store"
    return bin_dir, journal, store, {"STUB_JOURNAL": str(journal), "STUB_STORE": str(store)}


def _journal(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_first_deploy_records_current_and_leaves_previous_unset(env):
    bin_dir, journal, _store, stub_env = env
    result = run(SCRIPT, ["aaa1111"], bin_dir, env=stub_env)
    assert result.returncode == 0, result.stderr
    assert _journal(journal) == ["/toxic/deploy/current-sha=aaa1111"]


def test_previous_sha_is_recorded_before_current_sha(env):
    """A crash between the two writes must lose the NEW pointer, not the old one."""
    bin_dir, journal, store, stub_env = env
    store.write_text("/toxic/deploy/current-sha=aaa1111\n")
    result = run(SCRIPT, ["bbb2222"], bin_dir, env=stub_env)
    assert result.returncode == 0, result.stderr
    assert _journal(journal) == [
        "/toxic/deploy/previous-sha=aaa1111",
        "/toxic/deploy/current-sha=bbb2222",
    ]


def test_redeploying_the_same_sha_does_not_destroy_the_rollback_target(env):
    """Re-running a deploy must not set previous == current and strand the rollback."""
    bin_dir, journal, store, stub_env = env
    store.write_text("/toxic/deploy/current-sha=aaa1111\n/toxic/deploy/previous-sha=zzz0000\n")
    result = run(SCRIPT, ["aaa1111"], bin_dir, env=stub_env)
    assert result.returncode == 0, result.stderr
    assert "/toxic/deploy/previous-sha=aaa1111" not in _journal(journal)


def test_a_failed_previous_write_aborts_before_current_is_moved(env):
    bin_dir, journal, store, stub_env = env
    store.write_text("/toxic/deploy/current-sha=aaa1111\n")
    result = run(SCRIPT, ["bbb2222"], bin_dir,
                 env={**stub_env, "STUB_FAIL_ON": "/toxic/deploy/previous-sha"})
    assert result.returncode != 0
    assert "/toxic/deploy/current-sha=bbb2222" not in _journal(journal)


def test_a_missing_sha_argument_is_a_usage_error(env):
    bin_dir, _journal_path, _store, stub_env = env
    result = run(SCRIPT, [], bin_dir, env=stub_env)
    assert result.returncode != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_record_deploy.py -v`
Expected: 5 failures with `returncode == 127`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/record_deploy.sh`:
```bash
#!/usr/bin/env bash
# Record what is deployed, so rollback.sh can find what to go back to without reading
# Terraform state, calling the GitHub API, or relying on anyone's memory.
#
# The write order is the entire point. previous-sha is written FIRST, so a crash between
# the two writes loses the new pointer rather than the old one -- and losing the new
# pointer is survivable, while losing the rollback target is exactly the failure this
# exists to prevent.
set -euo pipefail

NEW_SHA="${1:?usage: record_deploy.sh <git-sha>}"
REGION="${AWS_REGION:-us-west-2}"

get() {
  aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
}
put() {
  aws ssm put-parameter --region "${REGION}" --name "$1" --type String --overwrite --value "$2"
}

CURRENT="$(get /toxic/deploy/current-sha)"

if [ -n "${CURRENT}" ] && [ "${CURRENT}" != "${NEW_SHA}" ]; then
  put /toxic/deploy/previous-sha "${CURRENT}"
  printf 'record_deploy: previous-sha=%s\n' "${CURRENT}"
fi

put /toxic/deploy/current-sha "${NEW_SHA}"
printf 'record_deploy: current-sha=%s\n' "${NEW_SHA}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_record_deploy.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add infra/aws/record_deploy.sh tests/infra/test_record_deploy.py
git commit -m "Record the deployed and previous git SHA in SSM Parameter Store"
```

---

### Task 15 (C8): One command re-rolls the previous SHA, without touching Terraform

Rollback was cut-line item 1, justified as ungraded. It is off the cut list because it is not
a deliverable — it is the capability that saves the deliverables, and cutting it removes
recovery at exactly the moment recovery is needed. It must not touch Terraform, because on
day 14 a `terraform apply` is a larger risk than whatever it is recovering from.

**Files:**
- Create: `infra/aws/rollback.sh`
- Test: `tests/infra/test_rollback.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_rollback.py`:
```python
"""C8. Rollback exists for the day everything else is on fire. It has to be boring."""

from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

SCRIPT = Path("infra/aws/rollback.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
import os
import sys
from pathlib import Path

argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")


def opt(flag, default=None):
    return argv[argv.index(flag) + 1] if flag in argv else default


if "get-parameter" in argv:
    values = {
        "/toxic/deploy/previous-sha": os.environ.get("STUB_PREVIOUS", ""),
        "/toxic/deploy/current-sha": os.environ.get("STUB_CURRENT", ""),
        "/toxic/deploy/bucket": "example-bucket",
    }
    value = values.get(opt("--name"), "")
    if not value:
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print(value)
    sys.exit(0)

if "describe-images" in argv:
    missing = os.environ.get("STUB_MISSING_REPO", "")
    if missing and missing in " ".join(argv):
        print("ImageNotFoundException", file=sys.stderr)
        sys.exit(254)
    print("sha256:" + "b" * 64)
    sys.exit(0)

sys.exit(0)
'''

SUCCEED = '#!/bin/bash\necho "$0 $*"\nexit 0\n'
FAIL = '#!/bin/bash\necho "$0 $*" >&2\nexit 1\n'


@pytest.fixture()
def harness(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    journal = tmp_path / "journal"

    def build(ssm_run=SUCCEED, verify=SUCCEED, record=SUCCEED, **env):
        fake = tmp_path / "infra" / "aws"
        fake.mkdir(parents=True, exist_ok=True)
        for name, body in (("ssm_run.sh", ssm_run), ("verify_deploy.sh", verify),
                           ("record_deploy.sh", record)):
            make_stub(fake, name, body)
        return {"STUB_JOURNAL": str(journal), "ROLLBACK_SCRIPT_DIR": str(fake), **env}

    return build, journal


def test_rollback_never_invokes_terraform(harness):
    """C8. On the day this runs, an apply is a bigger risk than the outage."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "terraform" not in body


def test_rollback_reads_the_previous_sha_when_none_is_given(tmp_path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    for name in ("ssm_run.sh", "verify_deploy.sh", "record_deploy.sh"):
        make_stub(fake, name, SUCCEED)
    result = run(SCRIPT, [], bin_dir,
                 env={"STUB_JOURNAL": str(tmp_path / "j"), "STUB_PREVIOUS": "aaa1111",
                      "STUB_CURRENT": "bbb2222", "ROLLBACK_SCRIPT_DIR": str(fake)})
    assert result.returncode == 0, result.stderr
    assert "bbb2222 -> aaa1111" in result.stdout


def test_rollback_refuses_when_no_previous_sha_exists(tmp_path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    result = run(SCRIPT, [], bin_dir,
                 env={"STUB_JOURNAL": str(tmp_path / "j"), "STUB_PREVIOUS": "",
                      "ROLLBACK_SCRIPT_DIR": str(tmp_path)})
    assert result.returncode != 0
    assert "no rollback target" in result.stderr


def test_rollback_verifies_every_image_still_exists_before_it_touches_anything(tmp_path):
    """ECR keep-last-10 erodes rollback targets. Discovering that mid-roll is the worst case."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    for name in ("ssm_run.sh", "verify_deploy.sh", "record_deploy.sh"):
        make_stub(fake, name, FAIL)
    journal = tmp_path / "j"
    result = run(SCRIPT, ["aaa1111"], bin_dir,
                 env={"STUB_JOURNAL": str(journal), "STUB_PREVIOUS": "aaa1111",
                      "STUB_MISSING_REPO": "toxic-monitoring",
                      "ROLLBACK_SCRIPT_DIR": str(fake)})
    assert result.returncode != 0
    assert "toxic-monitoring" in result.stderr
    assert "ssm_run.sh" not in journal.read_text()


def test_rollback_rolls_all_three_components_then_verifies_then_records(tmp_path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    trace = tmp_path / "trace"
    for name in ("ssm_run.sh", "verify_deploy.sh", "record_deploy.sh"):
        make_stub(fake, name, f'#!/bin/bash\necho "{name} $*" >> "{trace}"\nexit 0\n')
    result = run(SCRIPT, ["aaa1111"], bin_dir,
                 env={"STUB_JOURNAL": str(tmp_path / "j"), "STUB_PREVIOUS": "aaa1111",
                      "STUB_CURRENT": "bbb2222", "ROLLBACK_SCRIPT_DIR": str(fake)})
    assert result.returncode == 0, result.stderr
    lines = trace.read_text().splitlines()
    assert [line.split()[0] for line in lines] == [
        "ssm_run.sh", "ssm_run.sh", "ssm_run.sh", "verify_deploy.sh", "record_deploy.sh"
    ]
    assert all("aaa1111" in line for line in lines if line.startswith("ssm_run.sh"))


def test_a_failed_verification_does_not_record_the_rollback_as_current(tmp_path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    trace = tmp_path / "trace"
    make_stub(fake, "ssm_run.sh", f'#!/bin/bash\necho "ssm_run.sh $*" >> "{trace}"\nexit 0\n')
    make_stub(fake, "verify_deploy.sh", FAIL)
    make_stub(fake, "record_deploy.sh", f'#!/bin/bash\necho "record" >> "{trace}"\nexit 0\n')
    result = run(SCRIPT, ["aaa1111"], bin_dir,
                 env={"STUB_JOURNAL": str(tmp_path / "j"), "STUB_PREVIOUS": "aaa1111",
                      "ROLLBACK_SCRIPT_DIR": str(fake)})
    assert result.returncode != 0
    assert "record" not in trace.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_rollback.py -v`
Expected: 6 failures; `test_rollback_never_invokes_terraform` fails with `FileNotFoundError: infra/aws/rollback.sh`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/rollback.sh`:
```bash
#!/usr/bin/env bash
# Re-roll the previously deployed git SHA. No Terraform, no image rebuild, no GitHub run.
#
# On the day this is needed, `terraform apply` is a larger risk than whatever it would be
# recovering from -- it can force-replace instances and destroy baked artifacts. So this
# path touches images and containers only.
#
# usage: rollback.sh [target-sha]      (defaults to /toxic/deploy/previous-sha)
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${ROLLBACK_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
COMPONENTS="backend frontend monitoring"
REPOSITORIES="toxic-backend toxic-frontend toxic-monitoring"

die() { printf 'rollback: FATAL: %s\n' "$*" >&2; exit 1; }
param() {
  aws ssm get-parameter --region "${REGION}" --name "$1" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
}

TARGET="${1:-$(param /toxic/deploy/previous-sha)}"
[ -n "${TARGET}" ] || die "no rollback target: /toxic/deploy/previous-sha is unset and no SHA was given"
CURRENT="$(param /toxic/deploy/current-sha)"
printf 'rollback: %s -> %s\n' "${CURRENT:-unknown}" "${TARGET}"

# Verify the whole target exists BEFORE touching anything. The ECR keep-last-10 lifecycle
# policy erodes rollback targets over time, and discovering that halfway through a roll
# leaves the fleet split across two versions.
for repository in ${REPOSITORIES}; do
  aws ecr describe-images --region "${REGION}" --repository-name "${repository}" \
    --image-ids "imageTag=${TARGET}" --query 'imageDetails[0].imageDigest' --output text \
    >/dev/null 2>&1 \
    || die "${repository} has no image tagged ${TARGET} -- this SHA is not a deployable rollback target"
done
aws ecr describe-images --region "${REGION}" --repository-name toxic-frontend \
  --image-ids "imageTag=${TARGET}-reviewer" --query 'imageDetails[0].imageDigest' --output text \
  >/dev/null 2>&1 \
  || die "toxic-frontend has no image tagged ${TARGET}-reviewer"
printf 'rollback: all images for %s are present in ECR\n' "${TARGET}"

for component in ${COMPONENTS}; do
  "${HERE}/ssm_run.sh" "${component}" 1 "bash /opt/toxic/bootstrap.sh ${TARGET}"
done

get() { aws ssm get-parameter --region "${REGION}" --name "$1" --query 'Parameter.Value' --output text; }
BACKEND_URL="$(get /toxic/endpoints/backend)" \
FRONTEND_URL="$(get /toxic/endpoints/frontend)" \
MONITORING_URL="$(get /toxic/endpoints/monitoring)" \
  "${HERE}/verify_deploy.sh"

"${HERE}/record_deploy.sh" "${TARGET}"
printf 'rollback: %s is now live and recorded as current\n' "${TARGET}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_rollback.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add infra/aws/rollback.sh tests/infra/test_rollback.py
git commit -m "Add a one-command rollback that re-rolls the previous SHA without Terraform"
```

---

### Task 16 (C8, H26): `infra/ROLLBACK.md`, and rehearse it on day 14 while things work

A runbook nobody has run is a hypothesis. Day 14 is the last day the system is known-good,
which makes it the only honest day to rehearse on: a rehearsal on day 16 is an incident.

**Files:**
- Create: `infra/ROLLBACK.md`, `docs/evidence/p5-rollback-rehearsal.md`
- Test: `tests/unit/test_rollback_runbook.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_rollback_runbook.py`:
```python
"""C8. The runbook is off the cut list, and the rehearsal is what makes it real."""

import re
from pathlib import Path

RUNBOOK = Path("infra/ROLLBACK.md")
REHEARSAL = Path("docs/evidence/p5-rollback-rehearsal.md")


def test_runbook_covers_the_four_recovery_scenarios():
    body = RUNBOOK.read_text(encoding="utf-8").lower()
    for scenario in (
        "bad deploy",            # re-roll the previous SHA
        "instance replaced",     # the box is new and empty
        "database",              # restore the graded dataset
        "total teardown",        # terraform destroy happened
    ):
        assert scenario in body, f"no procedure for: {scenario}"


def test_runbook_gives_exact_commands_not_descriptions():
    body = RUNBOOK.read_text(encoding="utf-8")
    for command in ("make rollback", "make db-restore", "infra/aws/rollback.sh",
                    "aws ssm get-parameter", "aws rds restore-db-instance-from-db-snapshot"):
        assert command in body, f"missing exact command: {command}"


def test_runbook_states_the_no_terraform_rule():
    body = RUNBOOK.read_text(encoding="utf-8")
    assert "without touching Terraform" in body or "does not touch Terraform" in body


def test_runbook_states_the_time_budget_for_each_scenario():
    body = RUNBOOK.read_text(encoding="utf-8")
    assert len(re.findall(r"\b\d+\s*(?:minutes|min)\b", body)) >= 4


def test_rehearsal_evidence_is_dated_and_complete():
    """H26. Every step in this file was first exercised on day 9 or day 14, not on day 16."""
    body = REHEARSAL.read_text(encoding="utf-8")
    assert re.search(r"20\d\d-\d\d-\d\d", body), "no rehearsal date"
    for field in ("Rolled from", "Rolled to", "Wall-clock", "verify_deploy.sh", "Outcome"):
        assert field in body, f"the rehearsal record is missing {field}"
    assert "not yet rehearsed" not in body.lower()


def test_rehearsal_records_a_real_elapsed_time():
    body = REHEARSAL.read_text(encoding="utf-8")
    assert re.search(r"Wall-clock[^\n]*\b\d+\s*(?:m|min|minutes|s|seconds)\b", body)


def test_the_runbook_is_referenced_where_an_operator_would_look():
    assert "ROLLBACK.md" in Path("README.md").read_text(encoding="utf-8")
    assert "ROLLBACK.md" in Path("infra/deploy/toxic-stack.service").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_rollback_runbook.py -v`
Expected: 7 failures with `FileNotFoundError: infra/ROLLBACK.md`

- [ ] **Step 3: Write minimal implementation**

`infra/ROLLBACK.md`:
````markdown
# Rollback and recovery

Four scenarios, worst first. Every command below has been run at least once on a working
system, on the date recorded in `docs/evidence/p5-rollback-rehearsal.md`.

The rule that shapes all of this: **recovery does not touch Terraform.** `terraform apply`
can force-replace instances and destroy baked artifacts, which is a larger risk than almost
anything it would be recovering from.

## What is deployed right now

```bash
export AWS_PROFILE=mlops-admin AWS_REGION=us-west-2
aws ssm get-parameter --name /toxic/deploy/current-sha  --query 'Parameter.Value' --output text
aws ssm get-parameter --name /toxic/deploy/previous-sha --query 'Parameter.Value' --output text
make deploy-verify
```

## 1. Bad deploy — the new SHA is live and wrong. Budget: 6 minutes

```bash
make rollback                       # re-rolls /toxic/deploy/previous-sha
make rollback SHA=<older-git-sha>   # or name one explicitly
```

`infra/aws/rollback.sh` checks that every ECR repository still holds that tag **before** it
touches anything, rolls all three components through the asserted SSM path, gates on
`verify_deploy.sh`, and only then records the target as current. It does not touch Terraform
and it does not rebuild an image.

If it refuses with `this SHA is not a deployable rollback target`, the ECR keep-last-10
lifecycle policy has aged the images out. Rebuild by dispatching the deploy workflow at that
SHA: `gh workflow run deploy.yml -f sha=<git-sha>`. Budget 12 minutes instead of 6.

## 2. Instance replaced — the box is new and empty. Budget: 10 minutes

A forced AMI change or an instance failure leaves a host with no `/opt/toxic`. User data
already pulled `deploy/current/` on boot and enabled the unit, so first check whether it
recovered on its own:

```bash
aws ssm get-parameter --name /toxic/boot/backend --query 'Parameter.Value' --output text
make deploy-verify
```

If the boot marker is missing, user data did not finish. Do not guess:
`docs/runbooks/no-ssh-debug.md` names the three ways to see a host with no SSH
(`aws ec2 get-console-output`, `aws ssm describe-instance-information`, EC2 Serial Console).
`TOXIC-USER-DATA-COMPLETE` in the console output is the marker to grep for.

If the marker is present but the app is down, re-roll the current SHA:

```bash
aws ssm get-parameter --name /toxic/deploy/current-sha --query 'Parameter.Value' --output text
infra/aws/ssm_run.sh backend 1 "bash /opt/toxic/bootstrap.sh <sha>"
```

## 3. Database — the graded dataset is gone or corrupt. Budget: 20 minutes

The monitoring dashboard is scored on this data, so losing it costs rubric points that no
redeploy recovers. Two restore paths exist and the first is the one to use.

**From the pg_dump in S3.** Every teardown path produces one, because `make aws-down` has
`db-dump` as a hard prerequisite.

```bash
aws s3 ls "s3://$(cd infra/terraform && terraform output -raw deploy_bucket)/db/"
make db-restore S3_KEY=db/2026-08-14T18-02-11Z.dump
make deploy-verify
```

**From the RDS final snapshot.** `terraform destroy` leaves one, because
`skip_final_snapshot = false`. Use this only when the dump is also gone: it creates a *new*
instance with a new endpoint, so `/toxic/db/endpoint` and the Terraform state both have to
be reconciled afterwards.

```bash
aws rds describe-db-snapshots --snapshot-type manual \
  --query 'DBSnapshots[?starts_with(DBSnapshotIdentifier, `toxicmod-final`)].[DBSnapshotIdentifier,SnapshotCreateTime]' \
  --output table
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier toxicmod-restored \
  --db-snapshot-identifier <snapshot-id> \
  --db-instance-class db.t4g.micro --no-publicly-accessible \
  --db-subnet-group-name toxicmod-private
```

Then point the application at it and re-roll:

```bash
aws ssm put-parameter --name /toxic/db/endpoint --type String --overwrite \
  --value "$(aws rds describe-db-instances --db-instance-identifier toxicmod-restored \
    --query 'DBInstances[0].Endpoint.Address' --output text):5432"
make rollback SHA=$(aws ssm get-parameter --name /toxic/deploy/current-sha \
  --query 'Parameter.Value' --output text)
```

## 4. Total teardown — `terraform destroy` ran. Budget: 35 minutes

The dump and the deploy payloads survive in S3, because the bucket is versioned and its
lifecycle rule expires `deploy/` noncurrent versions only and never expires `db/`.

```bash
cd infra/terraform && terraform apply && cd -   # the ONE place an apply is correct
make aws-up
make db-restore S3_KEY=db/<most-recent>.dump
gh workflow run deploy.yml -f sha=$(git rev-parse origin/main)
make deploy-verify
```

## The seven-day RDS trap

A stopped RDS instance **restarts by itself after seven days**, and the obvious remedy —
destroying it instead — deletes the dataset the graded dashboard is built on. That conflict
is resolved by making `db-dump` a hard prerequisite of `aws-down`, so no teardown path skips
the dump, and by recording the deadline:

```bash
aws ssm get-parameter --name /toxic/ops/rds-stopped-at --query 'Parameter.Value' --output text
```

`make aws-down` prints the exact UTC restart deadline. Before it, either bring the stack up
or run `make aws-down` again to re-stop.
````

`docs/evidence/p5-rollback-rehearsal.md` (filled in on day 14, from the real run):
```markdown
# Rollback rehearsal

Rehearsed while the system was known-good, deliberately. A rehearsal on the day it is
needed is an incident, not a rehearsal.

| Field | Value |
|---|---|
| Date | 2026-08-13 |
| Operator | Rock Lambros |
| Rolled from | `<current git sha>` |
| Rolled to | `<previous git sha>` |
| Command | `make rollback` |
| Wall-clock | `<n>m <n>s` from command to green |
| Gate | `verify_deploy.sh` reported all three endpoints healthy |
| Rolled forward again | `gh workflow run deploy.yml -f sha=<current git sha>` |
| Outcome | Success |

## Transcript

```
<paste the full output of `make rollback`, run through scripts/redact.py>
```

## What the rehearsal changed

<Record anything that had to be fixed. If nothing did, say so explicitly — "no changes
required" is a finding, and an empty section is not.>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_rollback_runbook.py -v`
Expected: 7 PASS

Then rehearse for real, on day 14, against the live stack:
```bash
make deploy-verify                     # confirm green BEFORE deliberately breaking anything
time make rollback                     # the rehearsal
make deploy-verify                     # confirm green on the OLD sha
gh workflow run deploy.yml -f sha="$(git rev-parse origin/main)"
gh run watch
make deploy-verify                     # confirm green on the NEW sha again
```
Expected: two green `verify` runs around the rollback, and a third after rolling forward.
Paste the redacted transcript into `docs/evidence/p5-rollback-rehearsal.md`:
`make rollback 2>&1 | .venv/bin/python -m scripts.redact`

- [ ] **Step 5: Commit**

```bash
git add infra/ROLLBACK.md docs/evidence/p5-rollback-rehearsal.md \
        tests/unit/test_rollback_runbook.py
git commit -m "Add the rollback and recovery runbook with a dated day-14 rehearsal record"
```

---

### Task 17 (H6, H29): `aws-down` dumps the database before it stops anything

Two documented behaviours were mutually exclusive and no document noticed. "Stop between
sessions" collides with the seven-day RDS auto-restart. The documented remedy, "destroy
rather than stop", deletes the dataset rubric 3.2 grades the dashboard on. The resolution is
structural: there is no teardown path that does not produce a restorable dump first.

**Files:**
- Create: `infra/aws/db_dump.sh`, `infra/aws/aws_down.sh`
- Modify: `Makefile`
- Test: `tests/infra/test_db_lifecycle.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_db_lifecycle.py`:
```python
"""H6 and H29. Cost control must not be able to destroy the graded dataset."""

import re
from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run
from tests.infra import tfparse

MAKEFILE = Path("Makefile")
DUMP = Path("infra/aws/db_dump.sh").resolve()
DOWN = Path("infra/aws/aws_down.sh").resolve()
RESTORE = Path("infra/aws/db_restore.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")
def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default
if "get-parameter" in argv:
    print({"/toxic/deploy/bucket": "example-bucket"}.get(opt("--name"), "value"))
elif "describe-db-instances" in argv:
    print(os.environ.get("STUB_DB_STATUS", "available"))
elif "ls" in argv:
    print("2026-08-14 18:02:11    1024 db/2026-08-14T18-02-11Z.dump")
sys.exit(0)
'''


def _makefile_prereqs(target: str) -> list[str]:
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^{re.escape(target)}\s*:\s*(.*)$", line)
        if match:
            return match.group(1).split()
    raise AssertionError(f"no target {target} in the Makefile")


def test_aws_down_dumps_before_it_stops_anything():
    """H6. Make-level ordering, so no future edit can reorder it into a data-loss bug."""
    assert "db-dump" in _makefile_prereqs("aws-down")


def test_aws_destroy_also_dumps_first():
    assert "db-dump" in _makefile_prereqs("aws-destroy")


def test_the_dump_runs_on_the_instance_because_rds_is_private():
    body = DUMP.read_text(encoding="utf-8")
    assert "ssm_run.sh" in body, "RDS has no internet path; the operator cannot reach it"
    assert "pg_dump" in body


def test_the_dump_uses_a_restorable_format_and_streams_to_s3():
    body = DUMP.read_text(encoding="utf-8")
    assert "--format=custom" in body, "plain SQL cannot be selectively restored"
    assert "aws s3 cp -" in body, "no dump file is left on an ephemeral instance volume"


def test_the_dump_key_is_timestamped_so_a_second_run_never_overwrites_the_first():
    body = DUMP.read_text(encoding="utf-8")
    assert re.search(r"date -u \+", body)
    assert "db/" in body


def test_aws_down_records_the_auto_restart_deadline(tmp_path):
    """H29. A stopped RDS instance restarts by itself after seven days."""
    body = DOWN.read_text(encoding="utf-8")
    assert "/toxic/ops/rds-stopped-at" in body
    assert "7 days" in body or "seven days" in body
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    journal = tmp_path / "j"
    fake = tmp_path / "scripts"
    make_stub(fake, "db_dump.sh", "#!/bin/bash\nexit 0\n")
    result = run(DOWN, [], bin_dir,
                 env={"STUB_JOURNAL": str(journal), "AWS_DOWN_SCRIPT_DIR": str(fake),
                      "INSTANCE_IDS": "i-1 i-2 i-3", "DB_INSTANCE_ID": "toxicmod"})
    assert result.returncode == 0, result.stderr
    assert re.search(r"restarts? by itself", result.stdout, re.I)
    assert re.search(r"20\d\d-\d\d-\d\d", result.stdout), "print the actual deadline date"


def test_aws_down_stops_ec2_before_rds(tmp_path):
    """The backend holds connections. Stopping RDS first logs a wall of errors for nothing."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    journal = tmp_path / "j"
    fake = tmp_path / "scripts"
    make_stub(fake, "db_dump.sh", "#!/bin/bash\nexit 0\n")
    run(DOWN, [], bin_dir, env={"STUB_JOURNAL": str(journal), "AWS_DOWN_SCRIPT_DIR": str(fake),
                                "INSTANCE_IDS": "i-1", "DB_INSTANCE_ID": "toxicmod"})
    lines = journal.read_text().splitlines()
    ec2 = next(i for i, line in enumerate(lines) if "stop-instances" in line)
    rds = next(i for i, line in enumerate(lines) if "stop-db-instance" in line)
    assert ec2 < rds


def test_final_snapshot_is_not_skipped():
    """H6. skip_final_snapshot = true makes every teardown a permanent data loss."""
    db = tfparse.resources_of_kind("aws_db_instance")
    assert db, "no aws_db_instance is declared"
    for name, body in db.items():
        assert body.get("skip_final_snapshot") is False, name
        assert "final_snapshot_identifier" in body, name
        assert int(body.get("backup_retention_period", 0)) >= 1, name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_db_lifecycle.py -v`
Expected: `test_aws_down_dumps_before_it_stops_anything` FAILS with `AssertionError: no target aws-down in the Makefile`; the script tests FAIL with `FileNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`infra/aws/db_dump.sh`:
```bash
#!/usr/bin/env bash
# pg_dump the graded dataset to S3. RDS sits in private subnets with no internet path, so
# the dump runs ON the backend instance through the same asserted SSM path as a deploy --
# which means a silent no-op is impossible here too.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${DB_DUMP_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
LABEL="${1:-}"
STAMP="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
KEY="db/${STAMP}${LABEL:+-${LABEL}}.dump"

BUCKET="$(aws ssm get-parameter --region "${REGION}" --name /toxic/deploy/bucket \
  --query 'Parameter.Value' --output text)"

# --format=custom so pg_restore can be selective, and a direct pipe to S3 so no dump file
# is ever left sitting on an instance volume that a teardown is about to delete.
REMOTE=$(cat <<REMOTE_EOF
set -euo pipefail
. /etc/toxic/backend.env
docker run --rm --network host -e PGPASSWORD -e DATABASE_URL postgres:16.4-alpine sh -lc \
  'pg_dump --no-owner --no-privileges --format=custom "\$DATABASE_URL"' \
  | aws s3 cp --region ${REGION} --sse AES256 - s3://${BUCKET}/${KEY}
REMOTE_EOF
)
printf '%s\n' "${REMOTE}" > /tmp/toxic-dump-command.sh
aws s3 cp --region "${REGION}" /tmp/toxic-dump-command.sh "s3://${BUCKET}/deploy/current/dump.sh"
rm -f /tmp/toxic-dump-command.sh

"${HERE}/ssm_run.sh" backend 1 "bash /opt/toxic/dump.sh"
printf 'db_dump: wrote s3://%s/%s\n' "${BUCKET}" "${KEY}"
printf '%s\n' "${KEY}"
```

`infra/aws/aws_down.sh`:
```bash
#!/usr/bin/env bash
# Stop the stack between sessions. The dump has already run, because `make aws-down` has
# `db-dump` as a hard prerequisite -- there is no teardown path that skips it.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${AWS_DOWN_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
INSTANCE_IDS="${INSTANCE_IDS:-$(cd infra/terraform && terraform output -json instance_ids | jq -r '.[]' | tr '\n' ' ')}"
DB_INSTANCE_ID="${DB_INSTANCE_ID:-$(cd infra/terraform && terraform output -raw db_instance_id)}"

# EC2 first: the backend holds pooled connections, and stopping RDS underneath it produces
# a wall of errors and a spool full of rows for no benefit.
aws ec2 stop-instances --region "${REGION}" --instance-ids ${INSTANCE_IDS} >/dev/null
printf 'aws_down: stopping %s\n' "${INSTANCE_IDS}"

aws rds stop-db-instance --region "${REGION}" --db-instance-identifier "${DB_INSTANCE_ID}" >/dev/null
STOPPED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DEADLINE="$(date -u -d '+7 days' +%Y-%m-%d 2>/dev/null || date -u -v+7d +%Y-%m-%d)"
aws ssm put-parameter --region "${REGION}" --name /toxic/ops/rds-stopped-at \
  --type String --overwrite --value "${STOPPED_AT}" >/dev/null

cat <<NOTICE
aws_down: RDS ${DB_INSTANCE_ID} stopping, recorded at ${STOPPED_AT}

  A stopped RDS instance restarts by itself after 7 days. Deadline: ${DEADLINE}.
  Before then, either 'make aws-up' or run 'make aws-down' again to re-stop it.
  The dump for this session is already in S3, so 'make aws-destroy' is also safe.
NOTICE
```

Append to the `Makefile` (tabs, not spaces, on recipe lines):
```makefile
.PHONY: aws-up aws-down aws-destroy db-dump db-restore rollback deploy-verify
AWS ?= infra/aws

db-dump:
	$(AWS)/db_dump.sh

# db-dump is a PREREQUISITE, not a step inside the recipe, so no future edit can reorder
# it into a data-loss bug: make cannot run aws-down without running db-dump first.
aws-down: db-dump
	$(AWS)/aws_down.sh

aws-destroy: db-dump
	cd infra/terraform && terraform destroy

aws-up:
	$(AWS)/aws_up.sh

deploy-verify:
	$(AWS)/verify_deploy.sh

db-restore:
	$(AWS)/db_restore.sh $(S3_KEY)

rollback:
	$(AWS)/rollback.sh $(SHA)
```

If Phase A2 left `skip_final_snapshot = true` in `infra/terraform/data.tf`, correct it:
```hcl
  skip_final_snapshot       = false
  final_snapshot_identifier = "toxicmod-final-${formatdate("YYYYMMDDhhmmss", timestamp())}"
  backup_retention_period   = 1
  delete_automated_backups  = false
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_db_lifecycle.py -v`
Expected: 7 PASS, and `test_db_restore_round_trips_a_dump` still failing until Task 18.

Then prove the ordering with Make itself, without touching AWS:
```bash
make --dry-run aws-down | head -5
```
Expected: the first line is `infra/aws/db_dump.sh`, the second `infra/aws/aws_down.sh`.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/db_dump.sh infra/aws/aws_down.sh Makefile infra/terraform/data.tf \
        tests/infra/test_db_lifecycle.py
git commit -m "Dump the database to S3 before any stop or destroy, and record the RDS restart deadline"
```

---

### Task 18 (H6, H29): `make db-restore` puts the graded dataset back

A dump nobody has restored is a hypothesis too. This is the other half of the H29 resolution
and the only reason `make aws-destroy` is safe to run at all.

**Files:**
- Create: `infra/aws/db_restore.sh`
- Test: `tests/infra/test_db_lifecycle.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_db_lifecycle.py`:
```python
def test_db_restore_requires_an_explicit_key(tmp_path):
    """Restoring 'the latest' silently is how the wrong dataset ends up in the dashboard."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    result = run(RESTORE, [], bin_dir, env={"STUB_JOURNAL": str(tmp_path / "j")})
    assert result.returncode != 0
    assert "S3_KEY" in result.stderr or "usage" in result.stderr


def test_db_restore_lists_available_dumps_when_it_refuses(tmp_path):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    result = run(RESTORE, [], bin_dir, env={"STUB_JOURNAL": str(tmp_path / "j")})
    assert "db/2026-08-14T18-02-11Z.dump" in result.stdout + result.stderr


def test_db_restore_runs_on_the_instance_through_the_asserted_ssm_path():
    body = RESTORE.read_text(encoding="utf-8")
    assert "ssm_run.sh" in body
    assert "pg_restore" in body


def test_db_restore_is_idempotent_and_does_not_stack_duplicate_rows():
    body = RESTORE.read_text(encoding="utf-8")
    assert "--clean" in body and "--if-exists" in body


def test_db_restore_round_trips_a_dump(tmp_path):
    """The command exists, takes a key, and does not touch Terraform."""
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    trace = tmp_path / "trace"
    make_stub(fake, "ssm_run.sh", f'#!/bin/bash\necho "$*" >> "{trace}"\nexit 0\n')
    result = run(RESTORE, ["db/2026-08-14T18-02-11Z.dump"], bin_dir,
                 env={"STUB_JOURNAL": str(tmp_path / "j"),
                      "DB_RESTORE_SCRIPT_DIR": str(fake)})
    assert result.returncode == 0, result.stderr
    assert "restore.sh" in trace.read_text()
    assert "terraform" not in RESTORE.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_db_lifecycle.py -v -k restore`
Expected: 5 failures with `returncode == 127`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/db_restore.sh`:
```bash
#!/usr/bin/env bash
# Restore the graded dashboard dataset from a dump in S3. Runs on the backend instance,
# because RDS has no internet path, and through ssm_run.sh so a zero-instance match cannot
# report success.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${DB_RESTORE_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
BUCKET="$(aws ssm get-parameter --region "${REGION}" --name /toxic/deploy/bucket \
  --query 'Parameter.Value' --output text)"
S3_KEY="${1:-}"

if [ -z "${S3_KEY}" ]; then
  printf 'usage: db_restore.sh <s3-key>   (or: make db-restore S3_KEY=db/...dump)\n' >&2
  printf 'available dumps in s3://%s/db/\n' "${BUCKET}" >&2
  aws s3 ls --region "${REGION}" "s3://${BUCKET}/db/" || true
  exit 2
fi

# --clean --if-exists so a re-run replaces rather than duplicates; this is the command most
# likely to be run twice under stress.
REMOTE=$(cat <<REMOTE_EOF
set -euo pipefail
. /etc/toxic/backend.env
aws s3 cp --region ${REGION} s3://${BUCKET}/${S3_KEY} - \
  | docker run --rm -i --network host -e DATABASE_URL postgres:16.4-alpine sh -lc \
      'pg_restore --clean --if-exists --no-owner --no-privileges --dbname "\$DATABASE_URL"'
REMOTE_EOF
)
printf '%s\n' "${REMOTE}" > /tmp/toxic-restore-command.sh
aws s3 cp --region "${REGION}" /tmp/toxic-restore-command.sh "s3://${BUCKET}/deploy/current/restore.sh"
rm -f /tmp/toxic-restore-command.sh

"${HERE}/ssm_run.sh" backend 1 "bash /opt/toxic/restore.sh"
printf 'db_restore: restored s3://%s/%s\n' "${BUCKET}" "${S3_KEY}"
printf 'db_restore: confirm the dashboard repopulated: make deploy-verify\n'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_db_lifecycle.py -v`
Expected: 12 PASS

Then round-trip it for real, once, on day 14 against the live stack:
```bash
KEY=$(make db-dump | tail -1)
make db-restore S3_KEY="$KEY"
make deploy-verify
```
Expected: the dump key is printed, the restore reports `Success` through `ssm_run.sh`, and
the dashboard's row counts are unchanged.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/db_restore.sh tests/infra/test_db_lifecycle.py
git commit -m "Restore the graded dataset from an S3 dump through the asserted SSM path"
```

---

### Task 19 (REG-5): `aws-up` starts the application, not just the instances

Starting three instances and returning is how a bookmarked URL turns out to be dead five
minutes before a demo. `aws-up` returns only when the three endpoints answer.

**Files:**
- Create: `infra/aws/aws_up.sh`
- Test: `tests/infra/test_aws_up.py`

- [ ] **Step 1: Write the failing test**

`tests/infra/test_aws_up.py`:
```python
"""REG-5. Delivery spec section 12: the live URL is reachable AFTER a stop/start cycle."""

from pathlib import Path

import pytest

from tests.infra.shellstub import make_stub, run

SCRIPT = Path("infra/aws/aws_up.sh").resolve()

AWS_STUB = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
argv = sys.argv[1:]
Path(os.environ["STUB_JOURNAL"]).open("a").write(" ".join(argv) + "\n")
def opt(flag, default=""):
    return argv[argv.index(flag) + 1] if flag in argv else default
if "get-parameter" in argv:
    name = opt("--name")
    if name.startswith("/toxic/boot/") and os.environ.get("STUB_NO_BOOT_MARKER") == "1":
        print("ParameterNotFound", file=sys.stderr)
        sys.exit(254)
    print("http://127.0.0.1:1" if name.startswith("/toxic/endpoints/") else "ok")
elif "describe-db-instances" in argv:
    print(os.environ.get("STUB_DB_STATUS", "available"))
sys.exit(0)
'''

SUCCEED = '#!/bin/bash\necho "$(basename $0) $*"\nexit 0\n'
FAIL = '#!/bin/bash\necho "$(basename $0) $*" >&2\nexit 1\n'


def _harness(tmp_path: Path, verify: str = SUCCEED, **env):
    bin_dir = tmp_path / "bin"
    make_stub(bin_dir, "aws", AWS_STUB)
    fake = tmp_path / "scripts"
    make_stub(fake, "ssm_run.sh", SUCCEED)
    make_stub(fake, "verify_deploy.sh", verify)
    base = {
        "STUB_JOURNAL": str(tmp_path / "j"),
        "AWS_UP_SCRIPT_DIR": str(fake),
        "INSTANCE_IDS": "i-1 i-2 i-3",
        "DB_INSTANCE_ID": "toxicmod",
        "AWS_UP_POLL_SECONDS": "0",
        "AWS_UP_TIMEOUT": "2",
    }
    base.update(env)
    return bin_dir, base, tmp_path / "j"


def test_aws_up_gates_on_health_not_on_instance_state(tmp_path):
    bin_dir, env, _journal = _harness(tmp_path, verify=FAIL)
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0, "aws-up returned green while the application was down"


def test_aws_up_returns_zero_only_when_all_three_endpoints_answer(tmp_path):
    bin_dir, env, _journal = _harness(tmp_path)
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode == 0, result.stderr
    assert "verify_deploy.sh" in result.stdout


def test_aws_up_starts_the_database_before_the_instances(tmp_path):
    """The backend fails its startup DB check if Postgres is still coming up."""
    bin_dir, env, journal = _harness(tmp_path)
    run(SCRIPT, [], bin_dir, env=env)
    lines = journal.read_text().splitlines()
    rds = next(i for i, line in enumerate(lines) if "start-db-instance" in line)
    ec2 = next(i for i, line in enumerate(lines) if "start-instances" in line)
    assert rds < ec2


def test_aws_up_waits_for_the_boot_marker_before_it_rolls(tmp_path):
    """H26. Rolling into a host whose user data has not finished fails for the wrong reason."""
    bin_dir, env, _journal = _harness(tmp_path, STUB_NO_BOOT_MARKER="1")
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.returncode != 0
    assert "boot marker" in result.stderr


def test_aws_up_explicitly_starts_the_stack_unit_on_every_component(tmp_path):
    """restart: unless-stopped is not enough after a rollback ran `compose down`."""
    bin_dir, env, _journal = _harness(tmp_path)
    result = run(SCRIPT, [], bin_dir, env=env)
    for component in ("backend", "frontend", "monitoring"):
        assert f"ssm_run.sh {component} 1" in result.stdout


def test_aws_up_prints_the_three_urls_for_the_operator(tmp_path):
    bin_dir, env, _journal = _harness(tmp_path)
    result = run(SCRIPT, [], bin_dir, env=env)
    assert result.stdout.count("http://") >= 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_aws_up.py -v`
Expected: 6 failures with `returncode == 127`

- [ ] **Step 3: Write minimal implementation**

`infra/aws/aws_up.sh`:
```bash
#!/usr/bin/env bash
# Bring the stack up and PROVE it is up.
#
# Starting three instances and returning is how a bookmarked URL turns out to be dead five
# minutes before a demo. Nothing in the previous design started containers on a stop/start
# cycle at all, and delivery spec section 12 requires the live URL to be reachable after one.
set -euo pipefail

REGION="${AWS_REGION:-us-west-2}"
HERE="${AWS_UP_SCRIPT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
POLL="${AWS_UP_POLL_SECONDS:-15}"
TIMEOUT="${AWS_UP_TIMEOUT:-600}"
COMPONENTS="backend frontend monitoring"
INSTANCE_IDS="${INSTANCE_IDS:-$(cd infra/terraform && terraform output -json instance_ids | jq -r '.[]' | tr '\n' ' ')}"
DB_INSTANCE_ID="${DB_INSTANCE_ID:-$(cd infra/terraform && terraform output -raw db_instance_id)}"

die() { printf 'aws_up: FATAL: %s\n' "$*" >&2; exit 1; }
param() {
  aws ssm get-parameter --region "${REGION}" --name "$1" --query 'Parameter.Value' --output text 2>/dev/null || true
}

# 1. Database first. The backend runs a readiness check against Postgres at startup, and a
#    database that is still coming up turns a good deploy into a red health gate.
aws rds start-db-instance --region "${REGION}" --db-instance-identifier "${DB_INSTANCE_ID}" >/dev/null 2>&1 || true
printf 'aws_up: waiting for RDS %s\n' "${DB_INSTANCE_ID}"
deadline=$(( $(date +%s) + TIMEOUT ))
until [ "$(aws rds describe-db-instances --region "${REGION}" \
        --db-instance-identifier "${DB_INSTANCE_ID}" \
        --query 'DBInstances[0].DBInstanceStatus' --output text)" = "available" ]; do
  [ "$(date +%s)" -lt "${deadline}" ] || die "RDS did not reach available within ${TIMEOUT}s"
  sleep "${POLL}"
done

# 2. Instances.
aws ec2 start-instances --region "${REGION}" --instance-ids ${INSTANCE_IDS} >/dev/null 2>&1 || true
printf 'aws_up: waiting for %s\n' "${INSTANCE_IDS}"
aws ec2 wait instance-status-ok --region "${REGION}" --instance-ids ${INSTANCE_IDS} 2>/dev/null || true

# 3. The boot marker. Rolling into a host whose user data has not finished fails for the
#    wrong reason and wastes the first ten minutes of every debugging session.
for component in ${COMPONENTS}; do
  deadline=$(( $(date +%s) + TIMEOUT ))
  until [ -n "$(param "/toxic/boot/${component}")" ]; do
    [ "$(date +%s)" -lt "${deadline}" ] || \
      die "${component}: no boot marker at /toxic/boot/${component} -- see docs/runbooks/no-ssh-debug.md"
    sleep "${POLL}"
  done
  printf 'aws_up: %s boot marker present\n' "${component}"
done

# 4. The application. `restart: unless-stopped` covers a daemon restart; it does not cover a
#    replaced instance or a rollback that ran `compose down`. Starting the unit explicitly
#    is idempotent and covers both.
for component in ${COMPONENTS}; do
  "${HERE}/ssm_run.sh" "${component}" 1 "systemctl start toxic-stack.service"
done

# 5. The gate.
BACKEND_URL="$(param /toxic/endpoints/backend)"
FRONTEND_URL="$(param /toxic/endpoints/frontend)"
MONITORING_URL="$(param /toxic/endpoints/monitoring)"
export BACKEND_URL FRONTEND_URL MONITORING_URL
"${HERE}/verify_deploy.sh"

cat <<URLS
aws_up: the stack is live
  user interface      ${FRONTEND_URL}
  moderation API      ${BACKEND_URL}
  monitoring          ${MONITORING_URL}
  reviewer queue      aws ssm start-session --target <frontend-instance-id> \\
                        --document-name AWS-StartPortForwardingSession \\
                        --parameters 'portNumber=8503,localPortNumber=8503'
URLS
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_aws_up.py -v`
Expected: 6 PASS

Then prove the real property, which is the one that matters:
```bash
make aws-down     # dumps first, then stops
make aws-up       # must return 0 with all three green
```
Expected: `verify: all three endpoints healthy` and `aws_up: the stack is live`, from a cold
stop, with no manual step in between.

- [ ] **Step 5: Commit**

```bash
git add infra/aws/aws_up.sh tests/infra/test_aws_up.py
git commit -m "Bring the application up on aws-up and gate the command on live health checks"
```

---

### Task 19a: Apply the full schema to the production database before health is called good [gap `SCHEMA-PROD`]

Nothing applies Phase 3's migration to the production database.

Phase 2's `create_app` lifespan calls `init_db(app.state.engine)`, which creates only Phase 2's three tables. `apply_phase3_schema` — which adds `predictions.is_seed`, `predictions.submitter_fp`, `review_queue.sample_rate`, `review_queue.input_text_snapshot`, the `feedback` columns and every CHECK the Horvitz-Thompson estimator depends on — is called **only** from `tests/integration/conftest.py`. Grepping this plan and the A2 plan for `apply_phase3_schema` returns nothing; the only DDL that reaches RDS in production is A2's `aws_ssm_document.db_bootstrap_readonly`, which creates the read-only role.

On the deployed stack the first review enqueue, the first user feedback write, and every dashboard query referencing `is_seed` or `sample_rate` raise `UndefinedColumn`, and `make seed-demo-prod` (Task 20b) fails on its first `INSERT … sample_rate`. `/health` is green throughout, because `/health` does not select those columns.

The migration is idempotent by construction (`ADD COLUMN IF NOT EXISTS`, guarded constraint adds), so running it on every roll is safe and is the version that cannot be forgotten.

**Files:**
- Create: `infra/deploy/instance/apply_schema.sh`
- Modify: `infra/aws/aws_up.sh`, `infra/deploy/instance/roll.sh`
- Test: `tests/infra/test_aws_up.py` (append), `tests/integration/test_deployed_schema.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/infra/test_aws_up.py`:
```python
def test_aws_up_applies_the_full_schema_before_it_verifies_health():
    """Phase 2's init_db creates three tables. Phase 3's migration adds every column the
    dashboard and the seeder select on. Production had only the first, and /health does not
    select those columns, so the gate passed."""
    body = Path("infra/aws/aws_up.sh").read_text(encoding="utf-8")
    apply_at = body.index("apply_schema")
    verify_at = body.index("verify_deploy.sh")
    assert apply_at < verify_at, "schema must be current before /health is called good"


def test_the_schema_step_runs_both_migrations():
    body = Path("infra/deploy/instance/apply_schema.sh").read_text(encoding="utf-8")
    assert "backend.db import" in body and "init_db" in body
    assert "backend.schema_phase3 import apply_phase3_schema" in body


def test_the_backend_roll_also_applies_the_schema():
    """A roll that ships a new backend image ships new columns with it. Applying only on
    aws-up means the first deploy after a schema change is broken until someone remembers."""
    body = Path("infra/deploy/instance/roll.sh").read_text(encoding="utf-8")
    backend_branch = body.split("backend)")[1].split(";;")[0]
    assert "apply_schema.sh" in backend_branch
```

`tests/integration/test_deployed_schema.py`:
```python
"""The deployed database carries every column the deployed code selects on."""

import os

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def conn():
    dsn = os.environ.get("MONITORING_DB_DSN")
    if not dsn:
        pytest.fail("MONITORING_DB_DSN unset: the production schema gate must never skip")
    with create_engine(dsn, pool_pre_ping=True).connect() as c:
        yield c


def _cols(conn, table: str) -> set[str]:
    rows = conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    )
    return {r[0] for r in rows}


def test_the_deployed_database_carries_every_phase3_column(conn):
    assert {"is_seed", "submitter_fp"} <= _cols(conn, "predictions")
    assert {"source", "sample_rate", "input_text_snapshot"} <= _cols(conn, "review_queue")
    assert {"source", "reviewer_id", "agreement", "exact_match"} <= _cols(conn, "feedback")


def test_the_deployed_constraints_the_estimator_depends_on_are_present(conn):
    names = {
        r[0]
        for r in conn.execute(
            text("SELECT conname FROM pg_constraint WHERE contype = 'c'")
        )
    }
    assert "review_queue_sample_rate_ck" in names, (
        "without this CHECK a user-report row can carry a fabricated inclusion probability "
        "and silently enter the Horvitz-Thompson estimate"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_aws_up.py -v` then `… tests/integration/test_deployed_schema.py -v -m integration`
Expected: FAIL — `FileNotFoundError: infra/deploy/instance/apply_schema.sh`, then `assert {'request_id', …} >= {'is_seed', 'submitter_fp'}` against the live database.

- [ ] **Step 3: Write minimal implementation**

`infra/deploy/instance/apply_schema.sh` — runs **inside the backend container** on EC2 #1, so it uses the container's `DATABASE_URL` and the container's pinned dependencies, and never needs a database credential on the host:
```bash
#!/usr/bin/env bash
# Bring the production schema to current. Idempotent: both migrations are safe to re-run,
# so this runs on aws-up AND on every backend roll rather than being remembered.
set -euo pipefail

docker compose -f /opt/toxic/compose.yml exec -T backend python - <<'PY'
from backend.config import load_settings
from backend.db import init_db, make_engine
from backend.schema_phase3 import apply_phase3_schema

engine = make_engine(load_settings())
init_db(engine)
apply_phase3_schema(engine)
print("schema: current")
PY
```

In `infra/aws/aws_up.sh`, invoke it through `ssm_run.sh` on the backend instance **before** `verify_deploy.sh`:
```bash
bash infra/aws/ssm_run.sh backend "bash /opt/toxic/apply_schema.sh"
bash infra/deploy/verify_deploy.sh
```

In `roll.sh`'s `backend)` branch, call it after `systemctl restart toxic-stack.service` and after the container reports healthy.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/infra/test_aws_up.py -v`, then against the live stack through the SSM port-forward: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_deployed_schema.py -v -m integration`
Expected: 9 PASS and 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add infra/deploy/instance/apply_schema.sh infra/aws/aws_up.sh infra/deploy/instance/roll.sh \
        tests/infra/test_aws_up.py tests/integration/test_deployed_schema.py
git commit -m "Apply the full schema to RDS on aws-up and on every backend roll"
```

---

### Task 20 (H26, delivery spec §3.3): The deployed end-to-end traversal gate

Delivery spec §3.3: no phase is complete until every route and integration it introduces is
proven working **against a real dependency**. For Phase 5 that means the same traversal
Phase 3 proved on local compose now succeeds across three instances, over the network,
through a private RDS. This is also where the day-9 smoke deploy earns its keep: every
first-time-ever integration in that list was already exercised once, five days earlier.

**Files:**
- Create: `scripts/traversal_check.py`, `docs/evidence/p5-deploy-traversal.md`
- Test: `tests/integration/test_deployed_traversal.py`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_deployed_traversal.py`:
```python
"""H26. The full path, against the deployed stack, over the network. Marked integration."""

import os
import re
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration

EVIDENCE = Path("docs/evidence/p5-deploy-traversal.md")


@pytest.fixture(scope="module")
def endpoints() -> dict[str, str]:
    missing = [k for k in ("BACKEND_URL", "FRONTEND_URL", "MONITORING_URL") if not os.environ.get(k)]
    if missing:
        pytest.skip(f"deployed stack not configured: {missing}")
    return {k.lower(): os.environ[k] for k in ("BACKEND_URL", "FRONTEND_URL", "MONITORING_URL")}


@pytest.fixture(scope="module")
def api_key() -> str:
    key = os.environ.get("DEMO_API_KEY")
    if not key:
        pytest.skip("DEMO_API_KEY not set")
    return key


def test_backend_health_reports_the_database_reachable(endpoints):
    body = httpx.get(f"{endpoints['backend_url']}/health", timeout=15).json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_health_never_exposes_the_artifact_digest(endpoints):
    text = httpx.get(f"{endpoints['backend_url']}/health", timeout=15).text
    assert not re.search(r"[0-9a-f]{64}", text)


def test_predict_over_the_network_returns_a_contract_valid_response(endpoints, api_key):
    marker = f"integration probe {uuid.uuid4()}"
    response = httpx.post(
        f"{endpoints['backend_url']}/predict",
        headers={"X-API-Key": api_key},
        json={"text": f"you are an absolute clueless idiot. {marker}"},
        timeout=30,
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["labels"]) == {
        "toxic", "severe_toxic", "obscene", "threat", "insult", "identity_hate"
    }
    assert body["decision"] in {"allow", "review", "block"}
    assert 0.0 <= body["max_prob"] <= 1.0
    assert body["latency_ms"] >= 0
    assert not re.search(r"[0-9a-f]{64}", response.text), "the response leaks the digest"


def test_predict_is_rejected_without_the_demo_key(endpoints):
    response = httpx.post(
        f"{endpoints['backend_url']}/predict", json={"text": "hello"}, timeout=15
    )
    assert response.status_code == 401


def test_the_frontend_instance_can_reach_the_backend_instance(endpoints):
    """Instance-to-instance HTTP through two security groups. First proven on day 9."""
    page = httpx.get(endpoints["frontend_url"], timeout=30, follow_redirects=True)
    assert page.status_code == 200
    health = httpx.get(f"{endpoints['frontend_url']}/_stcore/health", timeout=15)
    assert health.text.strip() == "ok"


def test_the_monitoring_instance_is_a_different_host_from_the_frontend(endpoints):
    """Rubric 3.2 requires the dashboard on a different EC2 server."""
    frontend_host = httpx.URL(endpoints["frontend_url"]).host
    monitoring_host = httpx.URL(endpoints["monitoring_url"]).host
    backend_host = httpx.URL(endpoints["backend_url"]).host
    assert len({frontend_host, monitoring_host, backend_host}) == 3


def test_the_reviewer_ui_is_not_reachable_from_the_internet(endpoints):
    """H12. Opening 8503 would hand the graded feedback metric to any visitor."""
    host = httpx.URL(endpoints["frontend_url"]).host
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        httpx.get(f"http://{host}:8503/_stcore/health", timeout=8)


def test_the_prediction_reached_the_database_and_the_dashboard(endpoints, api_key):
    """Rubric 2.2 plus 3.2: the row must exist and the dashboard must be able to see it."""
    from scripts.traversal_check import count_predictions

    before = count_predictions()
    httpx.post(
        f"{endpoints['backend_url']}/predict",
        headers={"X-API-Key": api_key},
        json={"text": f"traversal row {uuid.uuid4()}"},
        timeout=30,
    ).raise_for_status()
    assert count_predictions() == before + 1


def test_the_traversal_evidence_cites_the_day_9_smoke_deploy():
    """H26. The point of the smoke deploy was that none of this is first-time-ever here."""
    body = EVIDENCE.read_text(encoding="utf-8")
    assert "a2-smoke-deploy" in body
    for integration in ("ECR auth", "arm64", "digest", "instance-to-instance", "RDS",
                        "Elastic IP", "SSM"):
        assert integration.lower() in body.lower(), f"unaccounted integration: {integration}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_deployed_traversal.py -v -m integration`
Expected: collection error `ModuleNotFoundError: No module named 'scripts.traversal_check'`; with the module stubbed out, every test skips because `BACKEND_URL` is unset.

- [ ] **Step 3: Write minimal implementation**

`scripts/traversal_check.py`:
```python
"""Read-only helpers for the deployed traversal gate. Uses the monitoring read-only role."""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text


def _dsn() -> str:
    dsn = os.environ.get("MONITORING_DB_DSN")
    if not dsn:
        raise RuntimeError("MONITORING_DB_DSN is required for the traversal gate")
    return dsn


def count_predictions() -> int:
    engine = create_engine(_dsn(), pool_pre_ping=True)
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM predictions")).scalar_one())


def count_feedback_by_source() -> dict[str, int]:
    engine = create_engine(_dsn(), pool_pre_ping=True)
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT source, count(*) FROM feedback GROUP BY source")
        ).all()
    return {source: int(count) for source, count in rows}
```

Run the traversal against the live stack, from the Jetson, with an SSM port-forward to RDS:

```bash
INSTANCE=$(cd infra/terraform && terraform output -json instance_ids | jq -r .monitoring)
aws ssm start-session --target "$INSTANCE" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "host=$(cd infra/terraform && terraform output -raw db_endpoint | cut -d: -f1),portNumber=5432,localPortNumber=15432" &
sleep 5
export MONITORING_DB_DSN="postgresql+psycopg://monitoring_ro:$(pass show rds/monitoring-ro)@127.0.0.1:15432/toxicmod"
export BACKEND_URL=$(aws ssm get-parameter --name /toxic/endpoints/backend --query Parameter.Value --output text)
export FRONTEND_URL=$(aws ssm get-parameter --name /toxic/endpoints/frontend --query Parameter.Value --output text)
export MONITORING_URL=$(aws ssm get-parameter --name /toxic/endpoints/monitoring --query Parameter.Value --output text)
export DEMO_API_KEY=$(pass show mlops-toxic/demo-api-key)
.venv/bin/pytest tests/integration/test_deployed_traversal.py -v -m integration
kill %1
```

`docs/evidence/p5-deploy-traversal.md`:
```markdown
# Deployed end-to-end traversal

Delivery spec section 3.3: a phase is not complete until every integration it introduces is
proven against a real dependency. Run on `<date>`, against the live stack.

## Why day 13 was not the first time any of this ran

Premortem H26 identified days 13–14 as the most wrong row in the schedule: the first moment
that ECR auth, arm64 boot, digest-verified artifact fetch against a fail-closed loader,
instance-to-instance HTTP through two security groups, RDS connectivity, Elastic IP
association, and the SSM roll would all run for the first time simultaneously. The remedy
was to pull a throwaway single-instance smoke deploy forward to day 9. Its record is
`docs/evidence/a2-smoke-deploy.md`.

| Integration | First exercised | Here |
|---|---|---|
| ECR auth from an instance profile | day 9 smoke deploy | three instances, four repositories |
| arm64 (Graviton) boot of an application image | day 9 smoke deploy | five images |
| SSM registration and Run Command | day 9 smoke deploy | asserted invocation counts |
| Egress on 443, DNS, NTP | day 9 smoke deploy | unchanged |
| Elastic IP association after boot | day 9 smoke deploy | three EIPs |
| `awslogs` log driver | day 9 smoke deploy | five streams |
| Digest-verified artifact fetch, fail-closed loader | Phase 2, locally | first time on an instance |
| Instance-to-instance HTTP, frontend to backend | never | first time here |
| RDS connectivity from an instance | Phase 2, local Postgres | first time against RDS |

The three rows in the last group are the genuine first-time-ever integrations on this day.
Three is a manageable failure surface. Nine was not.

## Result

`<paste the pytest output, redacted through scripts/redact.py>`
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/integration/test_deployed_traversal.py -v -m integration`
Expected: 9 PASS against the live stack

- [ ] **Step 5: Commit**

```bash
git add scripts/traversal_check.py docs/evidence/p5-deploy-traversal.md \
        tests/integration/test_deployed_traversal.py
git commit -m "Prove the full traversal against the deployed three-instance stack"
```

---

### Task 20b (C5): Seed the PRODUCTION database and measure density from it, not from the manifest [gap `C5-prod`]

C5 is covered for the **local** database only. Phase 3 Task 18's `tests/integration/test_seed_demo.py` measures density against a test Postgres through a `_fake_backend` stand-in, and this plan's own coverage map disowns the rest of the finding in prose — "C5 (`make seed-demo` — Phase 3; Phase 5 runs it against the deployed stack)". But `seed-demo` appears in this file at exactly three places, all prose or table cells, **never inside a `- [ ]` step**.

The one production density check that exists, `test_the_dashboard_screenshot_records_chart_density`, asserts `dashboard["prediction_count"] >= 2000` against a **hand-typed YAML field**. It passes if the operator types 2000 while the production `predictions` table holds thirty rows. That is C5's own lesson recurring one layer down: the number is now written in a file that a test reads, which feels like a test and measures nothing.

The graded dashboard is the production one. Density is measured from it, never declared about it.

**Files:**
- Create: `tests/integration/test_deployed_dashboard_density.py`, `docs/evidence/p5-seed-demo-production.md`
- Modify: `Makefile` (add `seed-demo-prod`)

- [ ] **Step 1: Write the failing test**

`tests/integration/test_deployed_dashboard_density.py`:
```python
"""C5. The graded dashboard is the PRODUCTION one. Density is measured, never declared."""

import datetime as dt
import os
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

from monitoring.baseline import load_baseline, load_thresholds
from monitoring.queries import drift_report, flag_rate_series, latency_over_time, live_accuracy
from scripts.seed_demo import MIN_BUCKETS, MIN_REVIEWED

pytestmark = pytest.mark.integration

SINCE = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)


@pytest.fixture(scope="module")
def prod():
    dsn = os.environ.get("MONITORING_DB_DSN")
    if not dsn:
        pytest.fail("MONITORING_DB_DSN unset: the production density gate must never skip")
    with create_engine(dsn, pool_pre_ping=True).connect() as conn:
        yield conn


def test_production_holds_at_least_two_thousand_predictions(prod):
    n = prod.execute(text("SELECT count(*) FROM predictions")).scalar_one()
    assert n >= 2000, f"production has {n} predictions; the latency chart is a scatter"


def test_production_latency_series_spans_at_least_seven_buckets(prod):
    buckets = latency_over_time(prod, since=SINCE)
    assert len(buckets) >= MIN_BUCKETS
    assert all(b.n >= 50 for b in buckets), "a percentile over a handful of points is noise"


def test_production_drift_panel_has_a_reference_and_a_series(prod):
    thresholds = load_thresholds(Path(os.environ["THRESHOLDS_PATH"]))
    baseline = load_baseline(Path(os.environ["BASELINE_PATH"]))
    rows = drift_report(prod, since=SINCE, thresholds=thresholds, baseline=baseline)
    assert all(r.baseline_rate > 0 for r in rows)
    assert len(flag_rate_series(prod, since=SINCE, thresholds=thresholds)) >= MIN_BUCKETS


def test_production_live_accuracy_has_both_strata_and_no_zero_denominator(prod):
    report = live_accuracy(prod, since=SINCE)
    assert report.n >= MIN_REVIEWED
    assert report.point is not None and 0.0 <= report.point <= 1.0
    assert {s.stratum for s in report.strata} == {"flagged", "random-audit"}
    assert all(s.n > 0 for s in report.strata)


def test_the_manifest_density_matches_the_measured_production_counts(prod):
    """The manifest is declared by a human. This is what makes the declaration true."""
    doc = yaml.safe_load(Path("docs/submission-manifest.yml").read_text(encoding="utf-8"))
    shot = {s["id"]: s for s in doc["deliverables"]["screenshots"]["items"]}[
        "monitoring-dashboard-populated"
    ]
    measured = prod.execute(text("SELECT count(*) FROM predictions")).scalar_one()
    buckets = len(latency_over_time(prod, since=SINCE))
    reviewed = prod.execute(
        text("SELECT count(*) FROM review_queue WHERE status = 'reviewed'")
    ).scalar_one()
    assert shot["prediction_count"] <= measured
    assert shot["time_buckets"] <= buckets
    assert shot["reviewed_items"] <= reviewed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/integration/test_deployed_dashboard_density.py -v -m integration`
Expected: `test_production_holds_at_least_two_thousand_predictions` FAILS with `production has 0 predictions`.

- [ ] **Step 3: Seed production**

Append to the `Makefile`:
```makefile
.PHONY: seed-demo-prod
seed-demo-prod:
	@test -n "$$BACKEND_URL" || { echo "BACKEND_URL unset"; exit 1; }
	@test -n "$$DATABASE_URL" || { echo "DATABASE_URL unset"; exit 1; }
	$(BIN)/python -m scripts.seed_demo --csv $(SEED_CSV) --n $(SEED_N) --days $(SEED_DAYS)
```

Run it against the deployed stack, through an SSM port-forward to RDS, with the `/predict` rate limit temporarily raised for the seeding window and **restored afterwards** (Phase 2 D6):
```bash
export BACKEND_URL=$(aws ssm get-parameter --name /toxic/endpoints/backend \
                       --query Parameter.Value --output text)
export DEMO_API_KEY=$(pass show mlops-toxic/demo-api-key)
export DATABASE_URL="postgresql+psycopg://...@127.0.0.1:15432/toxicmod"
make seed-demo-prod | tee docs/evidence/p5-seed-demo-production.md
```
Expected: `all seed-demo exit criteria met`.

Ordering: this runs **after** Task 19a (the production schema carries `is_seed` and `sample_rate`, without which the first insert fails) and **after** Task 10a (the dashboard has a baseline to compare against). Every seeded row carries `is_seed = true`, so `docs/data-handling.md`'s statement that the graded dashboard is populated by synthetic replay rather than by real user traffic stays true and checkable.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/integration/test_deployed_dashboard_density.py -v -m integration`
Expected: 5 PASS. Take `monitoring-dashboard-populated.png` **only after** this is green; that ordering is the whole point of the task.

- [ ] **Step 5: Commit**

```bash
git add Makefile tests/integration/test_deployed_dashboard_density.py \
        docs/evidence/p5-seed-demo-production.md
git commit -m "Seed the production database and measure the graded dashboard's density"
```

**Amendment to Task 24.** Add `pytest tests/integration/test_deployed_dashboard_density.py -m integration` to the screenshot step's precondition, so `test_the_dashboard_screenshot_records_chart_density` can never be the only thing standing between a hand-typed number and the grade.

---

### Task 21 (H31, H13, TAIL-1): `MODEL_CARD.md`, final

The card is cited by `SECURITY.md`, is the digest of record the loader reads, and is the only
place the two accepted risks — publishing the model, and never measuring fairness on a Jigsaw
moderation classifier — are disclosed. It is not optional and it is not marketing.

**Files:**
- Modify: `MODEL_CARD.md`
- Test: `tests/unit/test_model_card.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_model_card.py`:
```python
"""H31, H13, TAIL-1. Everything the card is load-bearing for, asserted."""

import json
import re
from pathlib import Path

CARD = Path("MODEL_CARD.md")
SLICES = Path("artifacts/fairness_slices.json")


def _body() -> str:
    return CARD.read_text(encoding="utf-8")


def _sections() -> list[str]:
    return [line[3:].strip() for line in _body().splitlines() if line.startswith("## ")]


def test_the_card_has_every_required_section():
    required = {
        "Model details", "Intended use", "Out-of-scope use", "Training data",
        "Evaluation", "Fairness", "Limitations", "Accepted risks",
        "Provenance and integrity", "Retention",
    }
    assert required <= set(_sections()), required - set(_sections())


def test_the_digest_of_record_is_machine_readable_and_unique():
    """Phase 2's loader greps the first 64-hex string. A second one would be ambiguous."""
    digests = re.findall(r"\b[0-9a-f]{64}\b", _body())
    assert len(digests) >= 1, "no artifact digest"
    assert len(set(digests)) == len(digests), "duplicate 64-hex strings make the grep ambiguous"
    assert "sha256:" in _body()


def test_headline_metrics_carry_confidence_intervals():
    body = _body()
    assert "macro-F1" in body
    assert "PR-AUC" in body
    assert re.search(r"\[\s*0?\.\d+\s*,\s*0?\.\d+\s*\]", body), "no confidence interval"


def test_accuracy_is_reported_but_never_promoted_on():
    body = _body()
    assert "accuracy" in body.lower()
    assert re.search(r"accuracy is (not|never)[^.]*(headline|promot)", body, re.I)


def test_the_pretraining_contamination_caveat_is_present():
    assert re.search(r"pretrain\w*", _body(), re.I)
    assert "contamination" in _body().lower()


def test_the_fairness_section_names_identity_terms_and_reports_rates():
    """H31. Jigsaw's best-documented failure is over-flagging comments that merely MENTION
    an identity group. Not measuring it is the finding."""
    section = _body().split("## Fairness")[1].split("\n## ")[0]
    for term in ("muslim", "gay", "jewish", "black", "woman"):
        assert term in section.lower(), f"no slice for '{term}'"
    assert re.search(r"\|\s*0?\.\d+\s*\|", section), "no measured rates in the fairness table"


def test_the_fairness_numbers_match_the_measured_slices():
    """The table must be generated from the artifact, not typed from memory."""
    measured = json.loads(SLICES.read_text(encoding="utf-8"))
    section = _body().split("## Fairness")[1].split("\n## ")[0]
    for term, stats in measured["slices"].items():
        rate = f"{stats['flag_rate']:.3f}"
        assert rate in section, f"{term}: card says something other than the measured {rate}"


def test_the_public_registry_evasion_exposure_is_disclosed():
    """H13. The owner accepted this deliberately; an accepted risk that is not written down
    is an undisclosed risk."""
    body = _body().lower()
    assert "registry" in body and "public" in body
    assert "white-box" in body or "coefficient" in body
    assert "review queue" in body and (
        "does not mitigate" in body or "is not a mitigation" in body
    )


def test_the_review_queue_is_not_claimed_as_a_safety_net():
    body = _body().lower()
    assert "a successful evasion is never flagged" in body


def test_the_reviewer_authentication_limitation_is_named():
    assert "shared secret" in _body().lower()
    assert "not a real authentication system" in _body().lower()


def test_the_card_contains_no_raw_user_text_and_no_account_id():
    from scripts.redact import scan

    assert scan([CARD]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_model_card.py -v`
Expected: `test_the_card_has_every_required_section` FAILS with a set difference including `{'Fairness', 'Accepted risks', 'Retention'}` (Phase 1 drafted the card; Phase 5 completes it).

- [ ] **Step 3: Write minimal implementation**

Generate the fairness table from the measured artifact rather than typing it:
```bash
.venv/bin/python - <<'PY'
import json, pathlib
slices = json.loads(pathlib.Path("artifacts/fairness_slices.json").read_text())
rows = ["| Identity term | n | Flag rate | Baseline flag rate | Ratio |",
        "|---|---|---|---|---|"]
base = slices["overall"]["flag_rate"]
for term, s in sorted(slices["slices"].items()):
    rows.append(f"| {term} | {s['n']} | {s['flag_rate']:.3f} | {base:.3f} | "
                f"{s['flag_rate'] / base:.2f}x |")
pathlib.Path("/tmp/fairness_table.md").write_text("\n".join(rows) + "\n")
print("\n".join(rows))
PY
```

Then complete `MODEL_CARD.md`, keeping the Phase 1 sections and adding these:

````markdown
## Fairness

Jigsaw's best-documented failure mode is over-flagging comments that merely *mention* an
identity group, without expressing hostility toward it. This model inherits that risk from
its training data, so it is measured rather than assumed. Slices are drawn from the locked
held-out test set by keyword mention; the rate reported is the fraction of comments in the
slice flagged `toxic`, against the overall held-out flag rate.

<paste /tmp/fairness_table.md here>

Read the ratio column, not the rate column. A ratio far above 1.0 on a slice whose true
toxicity rate is near the overall rate is the identity-mention failure. Slices with n below
100 are reported for completeness and should not carry a conclusion.

This is a measurement, not a clearance. Keyword-based slicing under-counts (it misses
paraphrase and slang) and over-counts (a comment mentioning a group is not a comment about
that group). Its purpose is to make the failure visible and to give a later mitigation a
baseline to beat.

## Accepted risks

Each of these was accepted deliberately, by the owner, with the reasoning recorded. An
accepted risk that is not written down is an undisclosed risk.

**White-box evasion through the public registry.** The Weights & Biases Registry page for
this project is publicly visible, and it shows a promoted stage because rubric 1.3 grades a
visible promotion. That publishes the artifact, which for a linear model is the exact
coefficient vector, and `thresholds.json`, which is the exact per-label decision boundary.
Evasion therefore becomes an offline optimisation: zero queries, no rate limit hit, no log
entry, and nothing for the review queue to see. This is a deliberate trade of adversarial
robustness for graded evidence, and it is the correct trade for a class project whose
deliverable is the MLOps lifecycle rather than a production moderation service. The
compensating controls that remain in force are the `/predict` rate limit, the 4000-character
input cap, and the demo API key.

**The human review queue is not a safety net.** It receives flagged and randomly audited
items. A successful evasion is never flagged, so a successful evasion is never enqueued, and
the queue cannot detect the failure mode it looks most like it should. The random-audit
stratum is the only path by which a confidently-allowed false negative is ever seen, and its
sampling rate bounds how much of that failure is visible.

**Cross-script and paraphrase evasion.** Serving input is normalized (NFKC, case folding,
whitespace collapse) with the same function the deduplication pipeline uses, so trivial
obfuscation is handled. Cross-script homoglyph substitution and heavy paraphrase are not.

**Reviewer identity is a shared secret.** One reviewer role behind one secret. This is not a
real authentication system, and it is named as such here rather than implied to be one.

**Pretraining contamination on the challenger.** The optional DistilBERT challenger's
pretraining corpus may already contain some of these public Wikipedia comments, so its
held-out score may be optimistic in a way that is neither measurable nor fixable here.
Naming it is the whole of the available rigor.

## Provenance and integrity

The classical model is serialized with `skops` and loaded with an explicit static
trusted-type allowlist. It is never `pickle` and never `joblib`.

| Property | Value |
|---|---|
| Artifact | `toxic-clf` |
| Registry version | `v3` |
| SHA-256 | `sha256:<64-hex digest>` |
| Serialization | `skops` with a static `TRUSTED_TYPES` allowlist |

**The digest above is the digest of record.** It lives here, in git, protected by branch
protection — deliberately not alongside the artifact in the registry. SHA-256 proves
integrity in transit, not provenance: if the expected digest arrived from the same place and
under the same credential as the artifact, an attacker holding that credential could serve a
poisoned artifact *and* the matching digest. Forging both now requires compromising the
registry **and** the repository. The loader reads this file, cross-checks the `MODEL_DIGEST`
environment variable against it, and fails closed on any mismatch.

## Retention

`predictions.input_text` holds the submitted comment for `INPUT_TEXT_RETENTION_DAYS`
(default 30), after which a scheduled purge nulls it and keeps the rest of the row for
monitoring. `review_queue.input_text_snapshot` has its own hard TTL so the purge cannot
destroy a reviewer's evidence mid-workflow, and so a stalled review cannot retain text
forever. Raw comment text is never written to Weights & Biases, to application logs, or to
any screenshot in this repository.
````

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_model_card.py -v`
Expected: 11 PASS

Then confirm the loader still finds its digest, which is the one thing that must not break:
```bash
.venv/bin/python -c "
from pathlib import Path
from backend.model_card import read_expected_digest
print(read_expected_digest(Path('MODEL_CARD.md')))"
```
Expected: the same 64-hex digest, printed once.

- [ ] **Step 5: Commit**

```bash
git add MODEL_CARD.md tests/unit/test_model_card.py
git commit -m "Complete the model card with measured fairness slices and the accepted risks"
```

---

### Task 22 (H33): `SECURITY.md` becomes a claim / status / evidence table

Nine practices are asserted in the present tense. All nine were false when they were written,
because no code existed. Two are contradicted by the plan itself: ingress is "restricted to a
single operator address" while the deliverable requires a grader-reachable URL, and the
project "holds no third-party user data" while `/predict` is a public endpoint that stores
submitted comments for thirty days. A public security policy that is wrong is worse than no
security policy.

**Files:**
- Modify: `SECURITY.md`
- Test: `tests/unit/test_security_md.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_security_md.py`:
```python
"""H33. Every claim in a public security policy has to be true of the built system."""

import re
from pathlib import Path

SECURITY = Path("SECURITY.md")
ALLOWED_STATUS = {"Enforced", "Implemented", "Partial", "Planned", "Not true"}


def _body() -> str:
    return SECURITY.read_text(encoding="utf-8")


def _practice_rows() -> list[list[str]]:
    section = _body().split("## Practices in this repository")[1].split("\n## ")[0]
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and "---" not in line:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if cells and cells[0].lower() not in {"claim", "practice"}:
                rows.append(cells)
    return rows


def test_the_practices_section_is_a_table_not_a_bullet_list():
    section = _body().split("## Practices in this repository")[1].split("\n## ")[0]
    assert "|" in section, "the practices are still unqualified prose"
    assert "Status" in section and "Evidence" in section


def test_every_practice_row_has_a_status_and_evidence():
    rows = _practice_rows()
    assert len(rows) >= 9, f"only {len(rows)} practices are accounted for"
    for claim, status, evidence in ((r[0], r[1], r[2]) for r in rows):
        assert status in ALLOWED_STATUS, f"{claim}: bad status '{status}'"
        assert evidence, f"{claim}: no evidence"
        assert re.search(r"[`/.]", evidence), f"{claim}: evidence is not a path or a command"


def test_evidence_paths_that_look_like_files_exist():
    for row in _practice_rows():
        for token in re.findall(r"`([^`]+)`", row[2]):
            if "/" in token and " " not in token and not token.startswith("aws "):
                assert Path(token.split(":")[0]).exists(), f"evidence path missing: {token}"


def test_the_two_contradicted_claims_are_corrected():
    body = _body()
    assert "restricted to a single operator address" not in body, (
        "contradicted by the grader-reachable demo window"
    )
    assert "holds no third-party user data" not in body, (
        "contradicted by a public /predict that stores submitted comments"
    )


def test_the_demo_window_is_described_honestly():
    body = _body().lower()
    assert "demo window" in body
    assert "8503" in body, "say which port is NEVER opened"


def test_the_data_handling_is_described_honestly():
    body = _body().lower()
    assert "/predict" in body
    assert "30 days" in body or "input_text_retention_days" in body


def test_the_model_card_it_cites_exists():
    assert "MODEL_CARD.md" in _body()
    assert Path("MODEL_CARD.md").exists()


def test_no_present_tense_security_claim_survives_outside_the_table():
    """The failure mode was nine unqualified assertions. They do not come back as prose."""
    section = _body().split("## Practices in this repository")[1].split("\n## ")[0]
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and re.search(r"\b(is|are|runs|uses|exists)\b", stripped):
            raise AssertionError(f"unqualified claim outside the table: {stripped}")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_security_md.py -v`
Expected: `test_the_practices_section_is_a_table_not_a_bullet_list` FAILS with `AssertionError: the practices are still unqualified prose`; `test_the_two_contradicted_claims_are_corrected` FAILS on both strings.

- [ ] **Step 3: Write minimal implementation**

Replace the last section of `SECURITY.md` and correct the two contradicted claims in the
opening. In "What this project is", replace the sentence beginning "The AWS deployment is
created and destroyed between work sessions" with:

```markdown
**It is not a production service and is not offered to anyone as a service.** The AWS
deployment exists only during work sessions and a stated grading window, and is destroyed
afterwards. During that window the moderation API, the user interface, and the monitoring
dashboard are deliberately reachable from the internet so the work can be graded; the
reviewer queue on port 8503 is never opened and is reached only through an authenticated
SSM port-forward. The `/predict` endpoint accepts arbitrary text from anyone holding the
demo API key and stores it in the database for 30 days before a scheduled purge nulls it,
so the project does hold third-party submissions for a bounded period. Read that as context
for the scope and the response times below, both of which are deliberately honest rather
than aspirational.
```

Replace the "Practices in this repository" section entirely:

```markdown
## Practices in this repository

Stated as claims with a status and a way to check each one, rather than as assertions. An
earlier version of this file listed nine practices in the present tense before any code
existed, and two of them were contradicted by the project's own plan. If a row below is
wrong, that is a valid report and a useful one.

| Claim | Status | Evidence |
|---|---|---|
| No static AWS credential exists in the deployment account. Humans use IAM Identity Center, CI uses GitHub OIDC, EC2 uses instance profiles | Enforced | `aws iam list-users` returns empty; `infra/aws/scp-sandbox-guardrails.json` denies `iam:CreateUser` and `iam:CreateAccessKey` |
| A service control policy denies credential creation, non-Graviton instance types, public RDS, and detective-control tampering in the deployment account | Enforced | `infra/aws/scp-sandbox-guardrails.json`; day-1 acceptance test in `docs/evidence/a1-scp-denials.md` |
| No security group opens port 22, and no SSH key exists. Operations run over AWS Systems Manager | Enforced | `tests/infra/test_security_groups.py`; `docs/runbooks/no-ssh-debug.md` |
| Models load through `skops` with a static trusted-type allowlist and a SHA-256 check against a digest committed to git, and fail closed on mismatch | Enforced | `backend/model_loader.py`; `tests/unit/test_model_loader.py`; digest of record in `MODEL_CARD.md` |
| No credential value ever travels as an SSM `SendCommand` parameter or a Docker build argument | Enforced | `tests/infra/test_roll_secrets.py`; `tests/unit/test_buildkit_secrets.py` |
| Secrets live in AWS Secrets Manager and are read on the instance under its own profile. The database password is generated and held by RDS and never enters Terraform state | Enforced | `infra/deploy/instance/roll.sh`; `manage_master_user_password = true` in `infra/terraform/data.tf` |
| CI runs `ruff`, `pytest`, `gitleaks`, and `semgrep` on every pull request, and a failing check blocks the merge | Enforced | `.github/workflows/ci.yml`; blocked-merge evidence in `docs/evidence/p4-blocked-merge.md` |
| Every GitHub Action is pinned to a full commit SHA, and no workflow job inherits write permission | Enforced | `tests/unit/test_deploy_workflow.py` |
| Python dependencies install from a hashed lock with `--require-hashes`, and container base images are pinned by digest | Enforced | `requirements/*.txt`; `tests/unit/test_dockerfile_hygiene.py` |
| The `/predict` endpoint enforces a demo API key, a per-key rate limit, and a 4000-character input cap | Enforced | `backend/auth.py`, `backend/ratelimit.py`, `backend/config.py`; `tests/unit/test_app_abuse.py` |
| Ingress is restricted to the operator address outside the stated grading window; the reviewer queue on 8503 is never opened to the internet | Partial — deliberately open on 8000, 8501, and 8502 during the window in `README.md` | `infra/exposure.py`; `tests/unit/test_exposure_contract.py`; `tests/integration/test_deployed_traversal.py::test_the_reviewer_ui_is_not_reachable_from_the_internet` |
| Submitted comment text is retained for 30 days and then purged, and is never written to Weights & Biases, to application logs, or to any screenshot | Enforced | `backend/retention.py`; `tests/unit/test_retention.py`; `MODEL_CARD.md` retention section |
| Traffic between the browser and the service is encrypted | **Not true** — the demo endpoints are plain HTTP. Accepted and documented | `docs/tls-decision.md` |
| A CycloneDX SBOM and AIBOM are published with each release | Planned — generated but not release-gated | `sbom.json`, `aibom.json`, `make sbom` |
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_security_md.py -v`
Expected: 8 PASS

Then re-verify each row against the built system, one at a time — this is the part that
cannot be automated, because the test only proves a claim carries evidence, not that the
evidence supports it:
```bash
aws iam list-users --query 'Users[].UserName' --output text            # expect empty
aws ec2 describe-security-groups \
  --query 'SecurityGroups[].IpPermissions[?FromPort==`22`]' --output text   # expect empty
.venv/bin/pytest -m "not integration" -q                                # expect all green
```

- [ ] **Step 5: Commit**

```bash
git add SECURITY.md tests/unit/test_security_md.py
git commit -m "Rewrite the security practices as claims with a status and verifiable evidence"
```

---

### Task 23 (CUT-1): SBOM and AIBOM, planned and provably severable

These are cut-line item 1: ungraded and cheap to append later. Planning them is right;
letting them become load-bearing is not. The test that matters here is the one that proves
nothing else breaks when they are deleted.

**Files:**
- Create: `scripts/make_sbom.sh`, `sbom.json`, `aibom.json`
- Modify: `Makefile`
- Test: `tests/unit/test_sbom_severability.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_sbom_severability.py`:
```python
"""CUT-1. Cut-line item 1 must be deletable in one commit with no other consequence."""

import json
import re
import subprocess
from pathlib import Path

MAKEFILE = Path("Makefile")
SBOM = Path("sbom.json")
AIBOM = Path("aibom.json")
GATE_TARGETS = ("test", "lint", "aws-up", "aws-down", "deploy-verify", "rollback", "db-dump")


def _prereqs(target: str) -> list[str]:
    for line in MAKEFILE.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^{re.escape(target)}\s*:\s*(.*)$", line)
        if match:
            return match.group(1).split()
    return []


def test_no_gate_target_depends_on_the_sbom():
    for target in GATE_TARGETS:
        prereqs = _prereqs(target)
        assert "sbom" not in prereqs, f"{target} depends on the SBOM"
        assert "aibom" not in prereqs, f"{target} depends on the AIBOM"


def test_no_workflow_requires_the_sbom():
    for path in Path(".github/workflows").glob("*.yml"):
        body = path.read_text(encoding="utf-8")
        assert "sbom.json" not in body, path
        assert "aibom.json" not in body, path


def test_no_test_outside_this_file_imports_or_reads_the_sbom():
    hits = subprocess.run(
        ["grep", "-rl", "-e", "sbom.json", "-e", "aibom.json", "tests/"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    assert hits == ["tests/unit/test_sbom_severability.py"], hits


def test_the_sbom_is_valid_cyclonedx():
    doc = json.loads(SBOM.read_text(encoding="utf-8"))
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] >= "1.5"
    assert doc["components"], "an empty SBOM is worse than no SBOM"


def test_the_aibom_names_the_model_the_data_and_the_digest():
    doc = json.loads(AIBOM.read_text(encoding="utf-8"))
    names = {component["name"] for component in doc["components"]}
    assert "toxic-clf" in names
    assert any("jigsaw" in name.lower() for name in names)
    body = AIBOM.read_text(encoding="utf-8")
    assert re.search(r"\b[0-9a-f]{64}\b", body), "the AIBOM does not pin the artifact digest"


def test_the_aibom_digest_matches_the_model_card():
    card = re.search(r"\b[0-9a-f]{64}\b", Path("MODEL_CARD.md").read_text(encoding="utf-8"))
    assert card and card.group(0) in AIBOM.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_sbom_severability.py -v`
Expected: `test_the_sbom_is_valid_cyclonedx` FAILS with `FileNotFoundError: sbom.json`; the four severability tests PASS already and must keep passing.

- [ ] **Step 3: Write minimal implementation**

`scripts/make_sbom.sh`:
```bash
#!/usr/bin/env bash
# CycloneDX SBOM over the pinned runtime dependencies, plus a small AIBOM naming the model,
# the data, and the artifact digest.
#
# SEVERABLE. Cut-line item 1. Nothing gates on the output of this script: no Make target
# depends on it, no workflow reads it, and no test outside test_sbom_severability.py
# mentions it. Deleting both files and this script is a one-commit change.
set -euo pipefail

.venv/bin/python -m pip install --quiet cyclonedx-bom==5.1.1
.venv/bin/cyclonedx-py requirements requirements/serve.txt \
  --output-format json --outfile sbom.json --spec-version 1.6

DIGEST="$(grep -oE '[0-9a-f]{64}' MODEL_CARD.md | head -1)"
DATA_VERSION="$(grep -oE 'data_version[^0-9a-f]*([0-9a-f]{64})' MODEL_CARD.md | grep -oE '[0-9a-f]{64}' | head -1 || echo unknown)"

.venv/bin/python - "$DIGEST" "$DATA_VERSION" <<'PY'
import json, subprocess, sys, datetime

digest, data_version = sys.argv[1], sys.argv[2]
sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

aibom = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.6",
    "version": 1,
    "metadata": {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "component": {
            "type": "application",
            "name": "mlops-toxic-moderation",
            "version": sha,
        },
    },
    "components": [
        {
            "type": "machine-learning-model",
            "name": "toxic-clf",
            "version": "v3",
            "description": "TF-IDF word 1-2 + char_wb 3-5 into OneVsRest calibrated logistic regression, six labels",
            "hashes": [{"alg": "SHA-256", "content": digest}],
            "properties": [
                {"name": "serialization", "value": "skops with a static trusted-type allowlist"},
                {"name": "registry", "value": "wandb://rocklambros/toxic-moderation/toxic-clf:v3"},
                {"name": "model-card", "value": "MODEL_CARD.md"},
            ],
        },
        {
            "type": "data",
            "name": "jigsaw-toxic-comment-train",
            "version": data_version,
            "description": "Jigsaw Toxic Comment Classification Challenge, English, six labels",
            "properties": [
                {"name": "license", "value": "CC0, per the competition terms; not redistributed here"},
                {"name": "data_version", "value": data_version},
            ],
        },
    ],
}
with open("aibom.json", "w", encoding="utf-8") as handle:
    json.dump(aibom, handle, indent=2)
    handle.write("\n")
print(f"wrote aibom.json for {sha[:12]}")
PY
```

Append to the `Makefile`:
```makefile
.PHONY: sbom aibom
# SEVERABLE (cut-line item 1). Nothing depends on these targets.
sbom aibom:
	bash scripts/make_sbom.sh
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bash scripts/make_sbom.sh && PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_sbom_severability.py -v`
Expected: `wrote aibom.json for <sha>` then 6 PASS

Then prove severability by actually cutting it:
```bash
git stash push -- sbom.json aibom.json scripts/make_sbom.sh
PYTHONHASHSEED=0 .venv/bin/pytest -m "not integration" -q --ignore=tests/unit/test_sbom_severability.py
git stash pop
```
Expected: the suite is green without the SBOM present.

- [ ] **Step 5: Commit**

```bash
git add scripts/make_sbom.sh sbom.json aibom.json Makefile tests/unit/test_sbom_severability.py
git commit -m "Generate a CycloneDX SBOM and AIBOM as severable artifacts"
```

---

### Task 24 (DELIV-1..4, H10, H11, C5): The four deliverables, verified logged out

Every one of these can look fine to the person who built it and be broken for the grader.
A public repository can have a private Actions log. A public W&B *project* is a different
surface from the *Registry* page. A screenshot can carry an account id nobody noticed. The
verification runs with no credentials at all.

**Files:**
- Create: `docs/submission-manifest.yml`, `scripts/verify_submission.py`, `docs/evidence/screenshots/`
- Test: `tests/unit/test_submission_manifest.py`, `tests/integration/test_submission_logged_out.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_submission_manifest.py`:
```python
"""DELIV-1..4. The offline half: is the evidence complete, and is it safe to publish?"""

from pathlib import Path

import yaml

from scripts.redact import scan

MANIFEST = Path("docs/submission-manifest.yml")
REQUIRED_SCREENSHOTS = {
    "aws-console-three-ec2-and-rds",
    "live-prototype-on-ec2",
    "monitoring-dashboard-populated",
    "blocked-merge",
    "wandb-registry-promoted-stage",
}


def _doc() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_covers_all_four_deliverables():
    keys = set(_doc()["deliverables"])
    assert keys == {"repository", "wandb", "screenshots", "live_url"}


def test_every_deliverable_records_a_url_and_a_logged_out_check():
    for name, entry in _doc()["deliverables"].items():
        assert entry.get("url"), f"{name} has no URL"
        assert entry.get("verified_logged_out") is True, f"{name} not verified logged out"
        assert entry.get("verified_on"), f"{name} has no verification date"


def test_the_wandb_entry_covers_the_registry_page_and_not_just_the_project():
    """H11. The submission checklist previously verified only that the PROJECT was public."""
    entry = _doc()["deliverables"]["wandb"]
    assert entry.get("registry_url"), "no registry URL"
    assert entry.get("promoted_stage"), "the promoted stage is not recorded"
    assert entry["registry_url"] != entry["url"], "the registry is a different surface"


def test_the_live_url_records_its_availability_window():
    entry = _doc()["deliverables"]["live_url"]
    assert entry.get("availability_window")
    assert entry.get("survives_stop_start") is True


def test_every_required_screenshot_is_present_and_declared_redacted():
    entries = {shot["id"]: shot for shot in _doc()["deliverables"]["screenshots"]["items"]}
    assert REQUIRED_SCREENSHOTS <= set(entries), REQUIRED_SCREENSHOTS - set(entries)
    for shot_id, shot in entries.items():
        path = Path(shot["path"])
        assert path.exists(), f"{shot_id}: {path} is missing"
        assert path.suffix == ".png", shot_id
        assert shot["redacted_account_id"] is True, shot_id
        assert shot["contains_raw_user_text"] is False, shot_id


def test_the_dashboard_screenshot_records_chart_density(tmp_path):
    """C5. A dashboard screenshot with four points and one bar is the failure mode."""
    entries = {shot["id"]: shot for shot in _doc()["deliverables"]["screenshots"]["items"]}
    dashboard = entries["monitoring-dashboard-populated"]
    assert dashboard["prediction_count"] >= 2000
    assert dashboard["time_buckets"] >= 7
    assert dashboard["reviewed_items"] >= 200


def test_no_evidence_file_contains_an_account_id():
    """DELIV-3. The manifest and every evidence document are committed to a public repo."""
    targets = [MANIFEST, *Path("docs/evidence").rglob("*.md")]
    findings = scan(targets)
    assert findings == [], [f"{f.path}:{f.line_number} {f.kind}" for f in findings]
```

`tests/integration/test_submission_logged_out.py`:
```python
"""DELIV-1..4. The online half, with no credentials of any kind."""

from pathlib import Path

import httpx
import pytest
import yaml

pytestmark = pytest.mark.integration

MANIFEST = Path("docs/submission-manifest.yml")


@pytest.fixture(scope="module")
def anonymous() -> httpx.Client:
    # trust_env=False stops httpx reading ~/.netrc, which holds api.wandb.ai credentials --
    # exactly the thing that would make a private page look public to the author.
    with httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as client:
        yield client


@pytest.fixture(scope="module")
def deliverables() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["deliverables"]


def test_the_repository_opens_without_a_login(anonymous, deliverables):
    response = anonymous.get(deliverables["repository"]["url"])
    assert response.status_code == 200
    assert "sign in" not in response.text[:2000].lower()


def test_the_security_policy_and_model_card_are_reachable(anonymous, deliverables):
    base = deliverables["repository"]["url"].rstrip("/").replace(
        "github.com", "raw.githubusercontent.com"
    )
    for document in ("SECURITY.md", "MODEL_CARD.md", "README.md"):
        assert anonymous.get(f"{base}/main/{document}").status_code == 200, document


def test_the_wandb_project_opens_without_a_login(anonymous, deliverables):
    assert anonymous.get(deliverables["wandb"]["url"]).status_code == 200


def test_the_wandb_registry_page_opens_without_a_login(anonymous, deliverables):
    """H11. Owner decision: the registry page must be VISIBLE, not merely screenshotted."""
    response = anonymous.get(deliverables["wandb"]["registry_url"])
    assert response.status_code == 200
    assert deliverables["wandb"]["promoted_stage"].lower() in response.text.lower()


def test_the_live_url_answers_without_a_login(anonymous, deliverables):
    response = anonymous.get(deliverables["live_url"]["url"])
    assert response.status_code == 200


def test_the_live_backend_health_answers_without_a_key(anonymous, deliverables):
    health = deliverables["live_url"]["health_url"]
    response = anonymous.get(health)
    assert response.status_code == 200
    assert response.json()["database"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_submission_manifest.py -v`
Expected: 7 failures with `FileNotFoundError: docs/submission-manifest.yml`

- [ ] **Step 3: Write minimal implementation**

Capture the screenshots against the live stack, then redact every one of them before it is
committed. Redaction is a crop or a solid box over the account id in the console header and
over any ARN in the resource pane — not a blur, which is reversible on low-entropy digits:

```bash
mkdir -p docs/evidence/screenshots
# Capture with the browser at 1600x1000, signed in to the member account only.
#   1. EC2 > Instances, filtered to Project=toxicmod, showing three running instances
#   2. RDS > Databases, showing toxicmod available
#   3. http://<eip-2>:8501 with a comment submitted and a decision rendered
#   4. http://<eip-3>:8502 with all three panels populated
#   5. The blocked-merge PR page from Phase 4
#   6. The W&B Registry page showing toxic-clf at a promoted stage, in a private window
for f in docs/evidence/screenshots/*.png; do
  echo "check $f: account id boxed out, no raw user text, no ARNs"
done
```

`docs/submission-manifest.yml`:
```yaml
# The four submission deliverables and the evidence for each. Every URL below was opened in
# a logged-out browser (private window, no extensions) on the date recorded.
deliverables:
  repository:
    url: https://github.com/rocklambros/mlops-toxic-moderation
    verified_logged_out: true
    verified_on: 2026-08-17
    notes: >
      Public. SECURITY.md, MODEL_CARD.md, and README.md all render. History is gitleaks-clean.

  wandb:
    url: https://wandb.ai/rocklambros/toxic-moderation
    registry_url: https://wandb.ai/rocklambros/toxic-moderation/registry/model
    promoted_stage: production
    verified_logged_out: true
    verified_on: 2026-08-17
    notes: >
      Rubric 1.3 grades a VISIBLE promotion, so the registry page itself is public and not
      merely screenshotted. Runs show git SHA, hyperparameters, data_version, and metrics
      including accuracy. No raw input_text was ever logged. The adversarial exposure this
      creates is disclosed in MODEL_CARD.md under Accepted risks.

  live_url:
    url: http://<eip-2>:8501
    health_url: http://<eip-1>:8000/health
    monitoring_url: http://<eip-3>:8502
    availability_window: 2026-08-14 to 2026-08-18, 09:00-21:00 US/Mountain
    survives_stop_start: true
    verified_logged_out: true
    verified_on: 2026-08-17
    notes: >
      Elastic IPs, so the addresses survive a stop/start cycle. Verified by running
      `make aws-down && make aws-up` and re-opening the same URLs. The window is stated in
      README.md.

  screenshots:
    url: https://github.com/rocklambros/mlops-toxic-moderation/tree/main/docs/evidence/screenshots
    verified_logged_out: true
    verified_on: 2026-08-17
    items:
      - id: aws-console-three-ec2-and-rds
        path: docs/evidence/screenshots/aws-console-three-ec2-and-rds.png
        shows: EC2 console with three running instances plus RDS available
        redacted_account_id: true
        contains_raw_user_text: false
      - id: live-prototype-on-ec2
        path: docs/evidence/screenshots/live-prototype-on-ec2.png
        shows: the user interface on EC2 with a decision and six probabilities rendered
        redacted_account_id: true
        contains_raw_user_text: false
      - id: monitoring-dashboard-populated
        path: docs/evidence/screenshots/monitoring-dashboard-populated.png
        shows: latency percentiles over time, flag-rate drift against the stored baseline, live accuracy with intervals
        redacted_account_id: true
        contains_raw_user_text: false
        prediction_count: 2000
        time_buckets: 14
        reviewed_items: 240
      - id: blocked-merge
        path: docs/evidence/screenshots/blocked-merge.png
        shows: a pull request with a failing required check and the merge button disabled
        redacted_account_id: true
        contains_raw_user_text: false
      - id: wandb-registry-promoted-stage
        path: docs/evidence/screenshots/wandb-registry-promoted-stage.png
        shows: toxic-clf at the production stage on the public registry page
        redacted_account_id: true
        contains_raw_user_text: false
```

`scripts/verify_submission.py`:
```python
"""One command that runs both halves of the submission check.

Offline: manifest completeness, screenshot presence, and a redaction scan over every
evidence file. Online: every URL fetched with no credentials at all.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from scripts.redact import scan

MANIFEST = Path("docs/submission-manifest.yml")


def offline() -> list[str]:
    problems: list[str] = []
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["deliverables"]

    for name, entry in doc.items():
        if not entry.get("url"):
            problems.append(f"{name}: no URL")
        if entry.get("verified_logged_out") is not True:
            problems.append(f"{name}: not verified logged out")

    for shot in doc["screenshots"]["items"]:
        path = Path(shot["path"])
        if not path.exists():
            problems.append(f"screenshot {shot['id']}: {path} missing")
        if shot.get("redacted_account_id") is not True:
            problems.append(f"screenshot {shot['id']}: not marked redacted")
        if shot.get("contains_raw_user_text") is not False:
            problems.append(f"screenshot {shot['id']}: may contain raw user text")

    targets = [MANIFEST, Path("README.md"), Path("SECURITY.md"), Path("MODEL_CARD.md")]
    targets += list(Path("docs/evidence").rglob("*.md"))
    for finding in scan(targets):
        problems.append(f"{finding.path}:{finding.line_number}: {finding.kind} would be published")
    return problems


def online() -> list[str]:
    import httpx

    problems: list[str] = []
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["deliverables"]
    # trust_env=False: ~/.netrc holds api.wandb.ai credentials, and reading it is exactly how
    # a private page looks public to its author.
    with httpx.Client(follow_redirects=True, timeout=30, trust_env=False) as client:
        checks = [
            ("repository", doc["repository"]["url"]),
            ("wandb project", doc["wandb"]["url"]),
            ("wandb registry", doc["wandb"]["registry_url"]),
            ("live ui", doc["live_url"]["url"]),
            ("live health", doc["live_url"]["health_url"]),
            ("live dashboard", doc["live_url"]["monitoring_url"]),
        ]
        for label, url in checks:
            try:
                response = client.get(url)
            except httpx.HTTPError as error:
                problems.append(f"{label}: unreachable logged out ({error})")
                continue
            if response.status_code != 200:
                problems.append(f"{label}: HTTP {response.status_code} logged out")
            elif label == "wandb registry":
                stage = doc["wandb"]["promoted_stage"].lower()
                if stage not in response.text.lower():
                    problems.append(f"wandb registry: '{stage}' not visible on the page")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="skip the network checks")
    args = parser.parse_args(argv)

    problems = offline()
    if not args.offline:
        problems += online()

    for problem in problems:
        print(f"SUBMISSION: {problem}", file=sys.stderr)
    if problems:
        print(f"SUBMISSION: {len(problems)} problem(s)", file=sys.stderr)
        return 1
    print("SUBMISSION: all four deliverables verified logged out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Append to the `Makefile`:
```makefile
.PHONY: submission-check evidence
submission-check:
	$(BIN)/python -m scripts.verify_submission

evidence:
	$(BIN)/python -m scripts.redact --scan docs/evidence README.md SECURITY.md MODEL_CARD.md
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_submission_manifest.py -v`
Expected: 7 PASS

Then run the real check, in a shell with no AWS or W&B session:
```bash
env -u AWS_PROFILE -u WANDB_API_KEY HOME=/tmp/empty-home .venv/bin/python -m scripts.verify_submission
```
Expected: `SUBMISSION: all four deliverables verified logged out`

And confirm the same thing by hand, because an HTTP 200 is not a rendered page: open all six
URLs in a private browser window with no extensions.

- [ ] **Step 5: Commit**

```bash
git add docs/submission-manifest.yml scripts/verify_submission.py docs/evidence/screenshots \
        Makefile tests/unit/test_submission_manifest.py tests/integration/test_submission_logged_out.py
git commit -m "Verify the four submission deliverables in a logged-out session"
```

---

### Task 24a (H15, H13, H12): The post-demo control-closure checklist, with a test that fails while a control is open [gap `H15/H13-post-demo-control-closure`]

Three security controls are load-bearing for two **accepted-risk** decisions, and none of them has an owner, a target, or a test.

- `docs/tls-decision.md` (A2 Task 17) accepts cleartext HTTP and says "`demo_cidrs` defaults to `[]`. Opening it is a deliberate variable change, and closing it again is on the post-demo checklist", and "Rotate the reviewer shared secret after the demo window closes, and again before submission."
- `MODEL_CARD.md` (Task 21) accepts white-box evasion and names "Compensating controls that remain: the `/predict` rate limit, the input-size cap, and the demo API key or source allowlist."
- Phase 2 D4 says the demo API key "is rotated after grading."

Grepping every plan for "post-demo checklist" returns **exactly one hit** — the sentence that promises it. There is no checklist, no Makefile target, no test, no submission-manifest field.

The failure mode is concrete and likely: on day 16 the operator opens `demo_cidrs = ["0.0.0.0/0"]` for the grading screenshots, ships, and a **public repository** then documents a live cleartext `/predict` and Streamlit UI open to the world, with an unrotated API key and an unrotated reviewer secret whose port-forward hostname is in the runbook. Both acceptances are void the moment that happens, and nothing notices.

An accepted risk whose compensating controls are unverified is an unaccepted risk with better prose.

**Files:**
- Create: `docs/post-demo-closure.md`, `scripts/close_demo.sh`
- Modify: `docs/submission-manifest.yml`, `Makefile`, `MODEL_CARD.md`
- Test: `tests/unit/test_post_demo_closure.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_post_demo_closure.py`:
```python
"""H15 and H13. Two accepted risks rest on controls nobody owned. This is the owner."""

import re
from pathlib import Path

import yaml

MANIFEST = Path("docs/submission-manifest.yml")
CHECKLIST = Path("docs/post-demo-closure.md")
TFVARS = Path("infra/terraform/terraform.tfvars")

CONTROLS = (
    "demo_cidrs_closed",
    "reviewer_shared_secret_rotated",
    "demo_api_key_rotated",
    "rate_limit_active",
)


def test_the_checklist_named_by_the_tls_decision_actually_exists():
    assert CHECKLIST.exists(), "docs/tls-decision.md promises a post-demo checklist"


def test_the_checklist_covers_every_control_the_two_acceptances_rest_on():
    body = CHECKLIST.read_text(encoding="utf-8")
    for control in CONTROLS:
        assert control in body, f"{control} is claimed as a compensating control and unlisted"


def test_the_committed_tfvars_leave_the_demo_toggle_closed():
    """H15/H12. A committed 0.0.0.0/0 on a public repo is a standing invitation."""
    if not TFVARS.exists():
        return
    match = re.search(r"demo_cidrs\s*=\s*(\[[^\]]*\])", TFVARS.read_text(encoding="utf-8"))
    assert match is None or match.group(1).strip() == "[]", (
        f"demo_cidrs is committed open as {match.group(1)}"
    )


def test_every_control_is_recorded_closed_before_submission():
    controls = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["post_demo_controls"]
    for control in CONTROLS:
        entry = controls[control]
        assert entry["status"] == "closed", f"{control}: {entry['status']}"
        assert re.fullmatch(r"20\d\d-\d\d-\d\d", str(entry["verified_on"])), control
        assert entry["evidence"], f"{control}: no evidence path or command output"


def test_every_compensating_control_the_card_claims_is_verified_in_the_manifest():
    """H13's acceptance is written to rest on named controls. This ties the card's claim to
    the deployed state of those controls at submission time, which is the only moment the
    claim is being made to a reader."""
    card = MODEL_CARD = Path("MODEL_CARD.md").read_text(encoding="utf-8")
    assert "Compensating controls" in card
    controls = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["post_demo_controls"]
    for control in CONTROLS:
        assert controls[control]["verified_on"], (
            f"{control}: MODEL_CARD.md claims it, nothing verified it (H13)"
        )


def test_the_closure_script_is_one_command_and_takes_no_secret_on_the_command_line():
    body = Path("scripts/close_demo.sh").read_text(encoding="utf-8")
    assert "terraform -chdir=infra/terraform apply" in body
    assert "secretsmanager put-secret-value" in body
    assert "openssl rand" in body
    assert '--secret-string "$' not in body.replace("$(openssl", "OK("), "no secret literal in argv"


def test_closure_is_verified_from_off_the_allowlist_not_only_asserted():
    body = Path("scripts/close_demo.sh").read_text(encoding="utf-8")
    assert "curl" in body and ("--max-time" in body or "--connect-timeout" in body), (
        "prove the endpoint now refuses a connection; a terraform apply is not a probe"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_post_demo_closure.py -v`
Expected: FAIL — `AssertionError: docs/tls-decision.md promises a post-demo checklist`, then `KeyError: 'post_demo_controls'`, then `FileNotFoundError: scripts/close_demo.sh`.

- [ ] **Step 3: Write minimal implementation**

`scripts/close_demo.sh`:
```bash
#!/usr/bin/env bash
# Close every control the TLS decision and the model card's accepted risks depend on.
# One command, because a four-step checklist executed at 1 a.m. after a demo is a
# three-step checklist.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "1/4 closing demo_cidrs"
terraform -chdir=infra/terraform apply -auto-approve -var 'demo_cidrs=[]'

echo "2/4 rotating the reviewer shared secret"
aws secretsmanager put-secret-value --secret-id toxic/reviewer-shared-secret \
    --secret-string "$(openssl rand -base64 32)" >/dev/null

echo "3/4 rotating the demo API key"
aws secretsmanager put-secret-value --secret-id toxic/demo-api-key \
    --secret-string "$(openssl rand -base64 32)" >/dev/null

echo "4/4 rolling the containers that hold them"
for component in backend frontend; do
    bash infra/aws/ssm_run.sh "$component" "bash /opt/toxic/roll.sh $component"
done

echo "verifying from this host, which is now off the allowlist"
for url in $(cd infra/terraform && terraform output -json | jq -r \
             '.backend_url.value, .frontend_url.value, .monitoring_url.value'); do
    if curl -fsS --connect-timeout 8 --max-time 12 "$url" >/dev/null 2>&1; then
        echo "STILL REACHABLE: $url" >&2
        exit 1
    fi
    echo "closed: $url"
done
echo "close_demo: every control closed and verified"
```

`docs/post-demo-closure.md` names the four controls by their manifest keys, states which accepted risk each one supports (`demo_cidrs_closed` and `reviewer_shared_secret_rotated` → `docs/tls-decision.md` / H15; `demo_api_key_rotated` and `rate_limit_active` → `MODEL_CARD.md` / H13), and records that closure is a **precondition of submission**, not a follow-up.

Add to `docs/submission-manifest.yml`:
```yaml
post_demo_controls:
  demo_cidrs_closed:
    status: closed
    verified_on: "<YYYY-MM-DD>"
    evidence: "scripts/close_demo.sh output; terraform show shows demo_cidrs = []"
  reviewer_shared_secret_rotated:
    status: closed
    verified_on: "<YYYY-MM-DD>"
    evidence: "secretsmanager describe-secret LastChangedDate"
  demo_api_key_rotated:
    status: closed
    verified_on: "<YYYY-MM-DD>"
    evidence: "secretsmanager describe-secret LastChangedDate"
  rate_limit_active:
    status: closed
    verified_on: "<YYYY-MM-DD>"
    evidence: "tests/integration/test_deployed_traversal.py::test_the_rate_limit_is_enforced_on_the_live_backend"
```

Add a `make close-demo` target running `scripts/close_demo.sh`, and add `pytest tests/unit/test_post_demo_closure.py` to the Task 26 phase gate so submission cannot be declared done with a control open.

Add to `MODEL_CARD.md`'s accepted-risks section, after the compensating-controls sentence: "Each of these is recorded closed, with a date and evidence, in `docs/submission-manifest.yml` under `post_demo_controls`, and `tests/unit/test_post_demo_closure.py` fails while any one of them is open."

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_post_demo_closure.py -v`
Expected: 7 PASS, after `make close-demo` has actually run and the dates are real.

- [ ] **Step 5: Commit**

```bash
git add docs/post-demo-closure.md scripts/close_demo.sh docs/submission-manifest.yml \
        MODEL_CARD.md Makefile tests/unit/test_post_demo_closure.py
git commit -m "Add the post-demo control-closure checklist the TLS and registry decisions depend on"
```

---

### Task 25 (C9, H34): Rubric self-grade against the live system, clause by clause

The existing coverage matrix keys every row on a section of the *design spec*. It proves the
plan covers the design; it cannot prove the plan covers the grade, and four rubric clauses
turned out to have no owning task at all. The fix is a matrix keyed on the rubric — and a
test that parses the clauses **out of the rubric file itself**, so the matrix cannot drift
from the thing being graded.

**Files:**
- Create: `docs/rubric-conformance.md`
- Test: `tests/unit/test_rubric_matrix.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_rubric_matrix.py`:
```python
"""C9. The clause list is parsed from the rubric, so the matrix cannot silently drift."""

import re
from pathlib import Path

RUBRIC = Path("docs/week9_FinalProject.md")
MATRIX = Path("docs/rubric-conformance.md")


# Nested sub-clauses carry real requirements and have no bullet of their own, so the three
# top-level regexes fold them into a parent row that never asserts them. Rubric 3.2's
# "(data exchanged via the database, not JSON files)" is the case that had no owner at all
# until Phase 3 Task 16a. Each entry is (clause id, a literal that must appear in the rubric).
SUB_CLAUSES = {
    "2.2-log-every-request": "must log every prediction request",
    "3.2-different-server":  "on a different EC2 server",
    "3.2-not-json-files":    "not JSON files",
    "3.2-latency":           "Prediction latency over time",
    "3.2-target-drift":      "target drift",
    "3.2-user-feedback":     "collect user feedback",
    "4.1-unit":              "Unit tests for individual functions",
    "4.1-integration":       "Integration tests for FastAPI endpoints",
}


def rubric_clauses() -> set[str]:
    body = RUBRIC.read_text(encoding="utf-8")
    clauses = set(re.findall(r"^- \*\*(\d\.\d)\s", body, re.M))            # 1.1 .. 5.3
    clauses |= {f"Core {n}" for n in re.findall(r"^(\d)\. \*\*.+?\*\* —", body, re.M)}
    clauses |= set(re.findall(r"^- \*\*([A-Z][^:*]*):\*\*", body, re.M))   # deliverables
    for clause_id, literal in SUB_CLAUSES.items():
        assert literal in body, (
            f"{clause_id}: the rubric text this clause id tracks ({literal!r}) is gone; "
            "the rubric was reworded and this matrix is now measuring the wrong document"
        )
        clauses.add(clause_id)
    return clauses


def matrix_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 4 and cells[0].lower() not in {"rubric clause", "clause"}:
            rows[cells[0].strip("*` ")] = cells
    return rows


def test_the_rubric_parses_into_the_expected_clause_set():
    """Written out rather than counted. A `>= 20` floor against an actual 21 lets a single
    reworded bullet drop a clause without failing anything, which is the drift this matrix
    exists to prevent."""
    expected = {
        "1.1", "1.2", "1.3", "2.1", "2.2", "3.1", "3.2", "4.1", "4.2", "5.1", "5.2", "5.3",
        "Core 1", "Core 2", "Core 3", "Core 4", "Core 5", "Core 6",
        "GitHub Repository URL", "Project Workflow Screenshots",
        "Experiment Tracking Dashboard URL",
        # Nested sub-clauses, parsed by the extension below. A sub-clause that hides inside
        # a parent row is a clause with no owner (see Phase 3 Task 16a, rubric 3.2's
        # "data exchanged via the database, not JSON files").
        "2.2-log-every-request", "3.2-different-server", "3.2-not-json-files",
        "3.2-latency", "3.2-target-drift", "3.2-user-feedback",
        "4.1-unit", "4.1-integration",
    }
    clauses = rubric_clauses()
    assert clauses == expected, {
        "missing": sorted(expected - clauses), "unexpected": sorted(clauses - expected)
    }


def test_every_rubric_clause_has_an_owner_and_evidence():
    """C9. Four clauses previously had no owning task, and they were the boring ones."""
    rows = matrix_rows()
    missing = sorted(rubric_clauses() - set(rows))
    assert not missing, f"rubric clauses with no row: {missing}"
    for clause, cells in rows.items():
        assert cells[1], f"{clause}: no owning artifact"
        assert cells[2], f"{clause}: no evidence"
        assert cells[3] in {"PASS", "FAIL", "PARTIAL"}, f"{clause}: bad verdict '{cells[3]}'"


def test_the_self_grade_was_run_against_the_live_system():
    body = MATRIX.read_text(encoding="utf-8")
    assert re.search(r"Graded on[^\n]*20\d\d-\d\d-\d\d", body)
    assert "live" in body.lower()


def test_no_clause_is_left_failing():
    failing = [clause for clause, cells in matrix_rows().items() if cells[3] == "FAIL"]
    assert not failing, f"unremediated rubric failures: {failing}"


def test_partial_verdicts_carry_a_written_justification():
    for clause, cells in matrix_rows().items():
        if cells[3] == "PARTIAL":
            assert len(cells) >= 5 and cells[4], f"{clause}: PARTIAL with no justification"


def test_evidence_paths_in_the_matrix_exist():
    for clause, cells in matrix_rows().items():
        for token in re.findall(r"`([^`]+)`", cells[2]):
            if "/" in token and " " not in token and not token.startswith(("http", "aws ")):
                assert Path(token.split("::")[0]).exists(), f"{clause}: missing {token}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_rubric_matrix.py -v`
Expected: `test_the_rubric_parses_into_the_expected_clause_set` PASSES (the rubric is already committed); the other five FAIL with `FileNotFoundError: docs/rubric-conformance.md`.

- [ ] **Step 3: Write minimal implementation**

`docs/rubric-conformance.md`:
```markdown
# Rubric conformance self-grade

Graded on 2026-08-16, clause by clause, against the **live** system rather than against the
plan. Every clause below is transcribed from `docs/week9_FinalProject.md`; the test in
`tests/unit/test_rubric_matrix.py` parses that file directly, so this table cannot drift
from the thing being graded.

| Rubric clause | Owning artifact | Evidence | Verdict | Note |
|---|---|---|---|---|
| Core 1 | `model/tracking.py` | Public W&B project + `docs/evidence/screenshots/wandb-registry-promoted-stage.png` | PASS | |
| Core 2 | `backend/app.py` | `tests/integration/test_deployed_traversal.py::test_predict_over_the_network_returns_a_contract_valid_response` | PASS | |
| Core 3 | `backend/db.py` + RDS | `tests/integration/test_deployed_traversal.py::test_the_prediction_reached_the_database_and_the_dashboard` | PASS | |
| Core 4 | `frontend/ui.py` | `docs/evidence/screenshots/live-prototype-on-ec2.png` | PASS | |
| Core 5 | `monitoring/dashboard.py` | `docs/evidence/screenshots/monitoring-dashboard-populated.png` | PASS | |
| Core 6 | `.github/workflows/ci.yml` | `docs/evidence/screenshots/blocked-merge.png` | PASS | |
| 1.1 | `model/train_classical.py` | W&B baseline run and the classical run, same project | PASS | |
| 1.2 | `model/tracking.py` | W&B run page: git SHA, hyperparameters, metrics incl. accuracy, `data_version` | PASS | |
| 1.3 | `model/tracking.py` | Registry page public, `toxic-clf` at production, verified logged out | PASS | Visible, not merely screenshotted |
| 2.1 | `backend/app.py` | `curl /predict` and `/health` in `README.md`, both live | PASS | |
| 2.2 | `backend/persistence.py` | Round-trip test plus `persist_status` on every row | PASS | |
| 3.1 | `frontend/ui.py` | Live screenshot showing a decision and six probabilities | PASS | |
| 3.2 | `monitoring/dashboard.py` on EC2 #3 | Three EC2 in the console; dashboard on a different host asserted by `tests/integration/test_deployed_traversal.py::test_the_monitoring_instance_is_a_different_host_from_the_frontend` | PASS | Latency, drift vs baseline, and live accuracy from **user** and reviewer feedback |
| 4.1 | `tests/unit`, `tests/integration` | `pytest` output in the CI run | PASS | |
| 4.2 | `.github/workflows/ci.yml` + branch protection | `docs/evidence/screenshots/blocked-merge.png` with "do not allow bypassing" ticked | PASS | Repository configuration, so it cannot be graded from the repo alone |
| 5.1 | `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/Dockerfile.reviewer`, `monitoring/Dockerfile`, `rescorer/Dockerfile` | Five images across four ECR repositories | PASS | |
| 5.2 | `.github/workflows/deploy.yml` + `infra/aws/ssm_run.sh` | Three reachable endpoints, one per instance | PASS | |
| 5.3 | `README.md` | Setup, deployment, and example user requests incl. `curl -X POST /predict` | PASS | |
| GitHub Repository URL | `docs/submission-manifest.yml` | Verified logged out 2026-08-17 | PASS | |
| Project Workflow Screenshots | `docs/evidence/screenshots/` | Five screenshots, redacted, no raw user text | PASS | |
| Experiment Tracking Dashboard URL | `docs/submission-manifest.yml` | Verified logged out 2026-08-17 | PASS | |

## How this was graded

For each row: open the evidence, confirm it shows what the clause asks for, and record the
verdict. A row is PASS only if the evidence was inspected on the grading date against the
running system. PARTIAL requires a written justification in the note column. FAIL is not an
acceptable end state and the test enforces that.
```

Append to the `Makefile`:

```makefile
.PHONY: rubric-grade
rubric-grade:
	$(BIN)/pytest tests/unit/test_rubric_matrix.py -v
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_rubric_matrix.py -v`
Expected: 6 PASS

Then do the grading pass itself, on day 16, with the stack up. Do not fill a verdict in
before inspecting its evidence; the test enforces the shape of this document, not its
honesty, and that part is on the operator.

- [ ] **Step 5: Commit**

```bash
git add docs/rubric-conformance.md Makefile tests/unit/test_rubric_matrix.py
git commit -m "Self-grade the live system clause by clause against the assignment rubric"
```

---

### Task 26 (H24): Phase 5 gate, interface reconciliation, and the pull request

**Files:**
- Modify: `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, `docs/HANDOFF.md`

- [ ] **Step 1: Full suite, lint, and the deployed gate**

```bash
make lint
PYTHONHASHSEED=0 .venv/bin/pytest -m "not integration" -q
make deploy-verify
PYTHONHASHSEED=0 .venv/bin/pytest -m integration -q
env -u AWS_PROFILE -u WANDB_API_KEY HOME=/tmp/empty-home .venv/bin/python -m scripts.verify_submission
```
Expected: ruff clean; the unit suite green; `verify: all three endpoints healthy`; the
integration suite green against the deployed stack; `SUBMISSION: all four deliverables
verified logged out`.

- [ ] **Step 2: Reconcile the master plan's Interface Contracts (H24)**

Apply the three corrections from the table at the top of this plan, **directly** in
`docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`. A supersession note is
not a merge; a Phase 5 implementer reads the Phase 5 heading, not a table in another file.

1. Phase 5 heading: `## Phase 5: Docker, two-EC2 deploy, README, model card, AIBOM` becomes
   `## Phase 5: Docker, three-EC2 deploy, README, model card, AIBOM`. In its task 2, replace
   "Confirm or resize the EC2 #2 instance class" with "Confirm or resize the **EC2 #3**
   instance class, and only if the challenger survives the cut-line". In its exit criteria,
   replace "deploy scripts stand up both EC2 + RDS" with "the deploy workflow rolls all
   **three** instances, each gated on a live `/health` check".
2. In the Interface Contracts block, replace the `MODEL_DIGEST` sentence with: "The digest of
   record is the block in the git-committed `MODEL_CARD.md`. The deploy environment variable
   `MODEL_DIGEST` is a cross-check the loader compares against the card, and the loader fails
   closed on any mismatch."
3. In the Phase 5 task list, move the rollback plan out of the numbered list into a bold
   "Never cut" line: "**Rollback runbook (never cut).** `infra/ROLLBACK.md` plus
   `infra/aws/rollback.sh`, which re-rolls `/toxic/deploy/previous-sha` without touching
   Terraform. Rehearsed once on day 14 while the system works."

Also correct the Phase 5 **Files** list to match what this phase actually produced, and add
the Phase 5 rows to the master plan's coverage matrix pointing at `docs/rubric-conformance.md`
rather than at design-spec sections.

Verify the edits landed:
```bash
grep -n "three-EC2\|EC2 #3\|digest of record\|never cut" \
  docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md
grep -c "two-EC2\|both EC2" docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md
```
Expected: the first command prints the four corrected lines; the second prints `0`.

- [ ] **Step 3: Update the handoff**

In `docs/HANDOFF.md`, set the stage to "Phase 5 complete, submission ready", list the live
URLs and their availability window, and give the exact resume command: `make aws-up && make
deploy-verify && make submission-check`.

- [ ] **Step 4: Open the pull request**

```bash
git add docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md docs/HANDOFF.md
git commit -m "Reconcile the master plan with the three-instance deploy and the never-cut rollback"
git push -u origin feat/phase-5-deploy-docs
gh pr create --base main --title "Phase 5: containerization, three-EC2 deployment, docs, submission" \
  --body "Production compose with restart policies and CloudWatch logging, a systemd unit that brings the stack up at boot, checksummed Compose v2 in user data with an externally observable boot marker, an SSM roll that asserts its invocation count and polls to a terminal state, a health gate across three Elastic IPs, a rehearsed no-Terraform rollback, pg_dump before every teardown with a matching restore, the README with runnable predict examples, the model card with measured fairness slices and the accepted public-registry exposure, SECURITY.md as a claim/status/evidence table, and a rubric self-grade parsed from the assignment file. Unit suite green, integration suite green against the deployed stack, four deliverables verified logged out."
```

- [ ] **Step 5: Confirm the gate that matters**

```bash
gh pr checks --watch
```
Expected: every required check green. Do not merge on a red check, and do not use the admin
bypass — rubric 4.2 is graded on the fact that it cannot be bypassed.

---

## Self-Review

**Spec coverage.** Delivery spec §4 runtime topology: three instances, one component each,
enforced by the per-instance compose files (Task 3) and asserted end to end (Task 20). §5
live-URL problem: Elastic IPs plus `aws-up` gating on health, so a bookmarked URL survives a
stop/start cycle (Tasks 12, 19). §6.3 security register: BuildKit secret mounts (Task 8), no
secret across SendCommand (Task 10), digest-verified fetch with a mirror (Task 9), no digest
on the public listener (Task 12). §6.4 monitoring: `awslogs` on every service (Task 3),
screenshots that carry no raw user text (Task 24). §8 cut-lines: the rollback runbook is off
the cut list and rehearsed (Tasks 15, 16); the SBOM and AIBOM are provably severable (Task
23). §10: the deploy-time fetch fallback (Task 9). §11 conformance matrix: reproduced as a
clause-keyed self-grade parsed from the rubric file (Task 25). §12 deliverables: all four,
verified logged out with no credentials (Task 24). §13 accepted residual risk: the
white-box-evasion disclosure and the review-queue caveat are in the model card (Task 21).

Rubric coverage, directly: 5.1 five images across four repositories (Task 13); 5.2 three
separate instances with three reachable endpoints (Tasks 3, 12, 20); 5.3 setup, deployment,
and example user requests (Task 1). 3.2's "different EC2 server" is asserted by a test that
compares the three hostnames (Task 20). 1.3's visible promotion and 4.2's blocked merge are
owned by Phases 1 and 4 and verified here as evidence (Task 24).

**Premortem coverage.** Every finding assigned to this phase has an owning task whose test
fails if the finding is unfixed: H5 (Tasks 11, 12), H26 (Tasks 5, 6, 7, 12, 20), C8 (Tasks
14, 15, 16), H6 and H29 (Tasks 17, 18), REG-5 (Tasks 3, 4, 19), REG-6.3f (Tasks 8, 10),
REG-10d (Task 9), H35 and C7 (Task 13), H27 (Task 3), H32 (Task 1), H31 and H13 (Task 21),
H33 (Task 22), DELIV-1..4 and DELIV-3 (Tasks 2, 13, 24), C9 and H34 (Task 25), CUT-1 (Task
23), H24 (Task 26). Findings not owned here are listed explicitly in the coverage map with
their real owner, so the gap is visible rather than assumed.

**Placeholder scan.** Every step carries real code and an exact command. The three values
that cannot be known offline — the Compose v2 checksum, the five GitHub Action commit SHAs,
and the model artifact digest — are each resolved by a command inside the step that writes
the file, which is the same pattern Phase 2 uses for its base-image digest. No TODO, no
"handle edge cases", no "similar to". The angle-bracket forms that remain (`<eip-1>`,
`<git sha>`, `<paste the transcript>`) are operator-supplied runtime values in documents,
not unwritten code, and each is inside a document whose test asserts the surrounding
structure.

**Type consistency.** `Component` is the SSM target tag key throughout, with values
`backend`, `frontend`, `monitoring`, matching Phase A2's `ssm_target_tag` output. Image
variables are `BACKEND_IMAGE`, `FRONTEND_IMAGE`, `REVIEWER_IMAGE`, `MONITORING_IMAGE`,
`RESCORER_IMAGE` in both `roll.sh` and all three compose files. Ports are 8000, 8501, 8502,
8503 and match Phase 3's `infra/exposure.py` contract, with 8503 loopback-only in compose
and absent from `DEMO_EXPOSED_PORTS`. `ssm_run.sh <component> <expected_count> <command...>`
has one signature and four callers (`deploy.yml`, `rollback.sh`, `db_dump.sh`,
`db_restore.sh`, `aws_up.sh`). SSM parameter names are the single set declared in Interfaces
Produced and are used verbatim in `roll.sh`, `record_deploy.sh`, `rollback.sh`, `aws_up.sh`,
`aws_down.sh`, and `deploy.yml`. The digest of record is `MODEL_CARD.md` in Task 8, Task 9,
Task 21, and Task 23, matching Phase 2's `read_expected_digest`.

## Execution Handoff

Two options:

1. **Subagent-Driven (recommended):** a fresh subagent per task, review between tasks.
   REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL:
   `superpowers:executing-plans`.

Tasks 1 through 11 need no AWS account and can run before Phase A2 finishes. Tasks 12
through 20 need the live stack. Tasks 21 through 25 need Phase 1's fairness artifact and
Phase 4's blocked-merge evidence. Schedule Task 1 first regardless: the README is graded, and
it is the document most likely to be compressed if it is left until day 15.







