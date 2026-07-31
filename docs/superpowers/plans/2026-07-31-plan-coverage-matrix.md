# Plan coverage matrix

**Date:** 2026-07-31
**Scope:** the eight phase plans in `docs/superpowers/plans/2026-07-31-*.md`, audited against the 47 surviving premortem findings and against every clause of `docs/week9_FinalProject.md`.

This document is the answer to one question: *for every finding and every graded clause, which task owns it, and which test goes red if it is unfixed?* A row with an owner and no failing test is a memo, which is the failure mode the premortem itself diagnosed most often. Rows are therefore keyed on the **test**, not on the prose.

Three independent completeness critics audited the plans. They agreed on 44 findings covered, 8 mentioned-but-untested, and 9 with no owning task at all. All 17 have been closed by the tasks named below. The critics' cross-plan contradictions — which would have made the suite unrunnable regardless of coverage — are in the third table.

---

## 1. Premortem findings

Severity is the premortem's own tier. "Owning task" names the file and the task number; tasks added by this audit are **bold**. "Test that fails if unfixed" names the assertion that goes red — where more than one exists, the most specific.

### Critical

| Id | Severity | Owning plan file | Task | Test that fails if unfixed |
|---|---|---|---|---|
| C1 | Critical | `2026-07-31-phase-0-data-firewall-v2.md` | 7 | `test_lsh_banding_reaches_99_percent_recall_at_the_operating_threshold` |
| C2 | Critical | `2026-07-31-phase-0-data-firewall-v2.md` | 15 | `test_gate_catches_the_band_dedup_deliberately_leaves` |
| C3 | Critical | `2026-07-31-phase-1-train-register.md` | 2, 3, 17 | `test_no_convergence_warning_is_emitted`, Task 17 perf gate |
| C4 | Critical | `2026-07-31-phase-1-train-register.md` | 4, 5, 6 | `test_the_outer_nesting_is_a_hard_crash_and_stays_documented`, `test_calibration_measurably_improves_reliability` |
| C5 (local) | Critical | `2026-07-31-phase-3-ui-monitoring-rescorer.md` | 17, 18 | `test_seed_demo_meets_every_exit_criterion` |
| **C5 (production)** | Critical | `2026-07-31-phase-5-deploy-docs.md` | **20b** | `test_production_holds_at_least_two_thousand_predictions`, `test_the_manifest_density_matches_the_measured_production_counts` |
| C6 | Critical | `2026-07-31-phase-a2-terraform.md` | 5, 6, **5a** | `test_every_app_group_can_reach_443_dns_and_ntp`, `test_every_group_including_reviewer_reaches_dns_and_ntp` |
| C7 | Critical | `2026-07-31-phase-a2-terraform.md`, `…phase-5-deploy-docs.md` | A2 3, A2 19, P5 13 | `test_deploy_workflow_never_runs_terraform_apply`, `test_docs_only_pushes_cannot_trigger_deploy` |
| C8 (rollback) | Critical | `2026-07-31-phase-5-deploy-docs.md` | 14, 15, 16 | `test_rollback_never_invokes_terraform`, `test_rehearsal_evidence_is_dated_and_complete` |
| **C8 (checkpoint)** | Critical | `2026-07-31-phase-3-ui-monitoring-rescorer.md` | **18b** | `test_every_checkpoint_in_the_delivery_spec_has_a_row`, `test_every_checkpoint_row_records_a_date_a_condition_and_a_decision` |
| C9 | Critical | `2026-07-31-phase-5-deploy-docs.md` | 25 | `test_every_rubric_clause_has_an_owner_and_evidence` |
| C10 | Critical | — (closed by fact) | — | Verified: `origin/main` carries the delivery spec; `infra/Makefile` is in no commit. **Residual:** all eight plans live only on `docs/owner-decisions-and-plans`, unmerged. Merge before implementation starts. |
| C11 (install hygiene) | Critical | `2026-07-31-phase-4-ci-gate.md`, `…phase-a1-account-bootstrap.md` | P4 1–3, A1 9 | `test_no_install_command_escapes_require_hashes`, `test_bootstrap_installs_nothing` |
| **C11 (credential scope)** | Critical | `2026-07-31-phase-a1-account-bootstrap.md` | 15, **15a** | `test_the_day_to_day_permission_set_is_not_administrator_access`, `test_an_idle_logout_target_exists` |

### High

| Id | Severity | Owning plan file | Task | Test that fails if unfixed |
|---|---|---|---|---|
| H1 | High | `…phase-0-data-firewall-v2.md`, `…phase-4-ci-gate.md` | P0 2, P4 4, 7 | `test_pytest_refuses_to_run_without_a_pinned_hash_seed`, `test_ci_sets_pythonhashseed` |
| H2 | High | `2026-07-31-phase-a2-terraform.md` | 13 | `test_every_instance_is_in_the_scp_allowlist` |
| H3 | High | `2026-07-31-phase-a1-account-bootstrap.md` | 3, 16 | live probe `H3-t4g-xlarge-deny` plus four real allowed launches |
| H4 | High | `2026-07-31-phase-a2-terraform.md` | 12 | `test_the_trust_policy_ands_the_repo_and_the_ref` (asserted on HCL and on `terraform show -json`) |
| H5 | High | `2026-07-31-phase-5-deploy-docs.md` | 11, 12 | `test_zero_matching_instances_fails_the_deploy`, `test_verify_fails_when_one_endpoint_is_down` |
| H6 | High | `…phase-5-deploy-docs.md`, `…phase-a2-terraform.md` | P5 17, 18; A2 9 | `test_aws_down_dumps_before_it_stops_anything`, `test_final_snapshot_is_not_skipped` |
| **H7** | High | `2026-07-31-phase-a2-terraform.md`, `…phase-5-deploy-docs.md` | A2 16, **P5 1a** | `test_cost_model_prices_every_previously_omitted_line_item`, `test_readme_cost_agrees_with_the_cost_model`, `test_the_delivery_spec_no_longer_carries_the_superseded_hourly_figure` |
| H8 | High | `…phase-2-backend-rds.md`, `…phase-3-…` | P2 9, 10, **10a**; P3 1, 2, 15 | `test_horvitz_thompson_differs_from_the_unweighted_pool`, `test_a_design_stratum_row_cannot_omit_its_sample_rate` |
| H9 | High | `2026-07-31-phase-3-ui-monitoring-rescorer.md` | 6, 8, 10 | `test_user_feedback_writes_a_user_sourced_row` |
| H10 | High | `2026-07-31-phase-4-ci-gate.md` | 12, 13 | `test_administrators_cannot_bypass`, `test_the_blocked_merge_api_refusal_is_recorded` |
| H11 | High | `2026-07-31-phase-1-train-register.md` | 7, 15 | `test_feature_footprint_is_under_the_memory_budget`, unauthenticated GraphQL registry check |
| H12 | High | `…phase-3-…`, `…phase-a2-terraform.md` | P3 11, 12; **A2 5a** | `test_reviewer_ui_is_operator_only`, `test_no_ingress_rule_of_any_kind_anywhere_reaches_8503` |
| **H13** | High (accepted) | `2026-07-31-phase-5-deploy-docs.md` | 21, **24a** | `test_the_public_registry_evasion_exposure_is_disclosed`, `test_every_compensating_control_the_card_claims_is_verified_in_the_manifest` |
| H14 | High | `2026-07-31-phase-2-backend-rds.md` | 7, 15, 17, 18 | `test_no_response_ever_carries_the_artifact_digest` |
| **H15** | High (accepted) | `2026-07-31-phase-a2-terraform.md`, `…phase-5-…` | A2 17, **A2 5a**, **P5 24a** | `test_no_ingress_rule_of_any_kind_anywhere_reaches_8503`, `test_the_committed_tfvars_leave_the_demo_toggle_closed` |
| **H16** | High | `2026-07-31-phase-a2-terraform.md` | 10, 11, **5a**, **11 amended** | `test_no_ui_tier_can_read_the_rds_master_secret`, `test_only_the_backend_and_monitoring_tiers_may_reach_5432` |
| H17 | High | `2026-07-31-phase-a1-account-bootstrap.md` | 6, 16 | live probes `H17-updatetrail-deny`, `H17-guardduty-deny` |
| H18 | High | `2026-07-31-phase-a1-account-bootstrap.md` | 5, 8, 16 | six-cell `iam simulate-custom-policy` matrix plus two real `create-db-instance` attempts |
| H19 | High | `2026-07-31-phase-a1-account-bootstrap.md` | 4, 7, 16 | `H19-createfleet-deny`, region-lock deny/allow pair |
| H20 | High | `2026-07-31-phase-0-data-firewall-v2.md` | 4, 8, 9, 14, 16, 17 | `test_raw_sha256_is_the_digest_of_the_file_on_disk` |
| **`data_version` split** | High (H20 vectorization) | `2026-07-31-phase-1-train-register.md` | **14a** | `test_config_carries_all_three_provenance_fields_not_one_composite`, `test_build_run_config_refuses_a_bare_string_data_version` |
| H21 | High | `2026-07-31-phase-0-data-firewall-v2.md` | 5, 10 | seed-parametrized split; every label in every fold and in the test set |
| H22 | High | `…phase-0-…`, `…phase-2-…` | P0 12, P2 8 | `test_severe_toxic_forces_toxic_before_the_response_is_built` |
| H23 | High | `2026-07-31-phase-0-data-firewall-v2.md` (owner) | P0 12; **P4 11** | `test_backend_never_re_derives_the_label_zip`, `test_probs_to_dict_is_defined_exactly_once` |
| **H24** | High | `…phase-0-…` 18 (authoritative), `…phase-4-…` **11 corrected** | P0 18, P4 11 | `test_dataset_bundle_fields_match_the_documented_block`, `test_there_is_exactly_one_interface_contract_conformance_suite` |
| **H25** | High | `…phase-0-…` 3, `…phase-2-…` **4a** | P0 3, P2 4a | `test_the_serving_path_uses_the_declared_serving_normalizer`, `test_the_input_cap_has_one_source_of_truth` |
| H26 | High | `2026-07-31-phase-5-deploy-docs.md` | 5, 6, 7, 12, 20 | `test_boot_marker_is_the_last_action`, `test_traversal_evidence_cites_the_day_9_smoke_deploy` |
| **H27** | High | `…phase-5-…` 3, `…phase-a2-…` 8, 14, **14a** | P5 3; A2 8, 14, 14a | `test_every_production_service_ships_logs_to_cloudwatch`, `test_the_alerts_topic_has_at_least_one_confirmed_subscriber`, `test_alarm_delivery_was_proven_end_to_end` |
| H28 | High | `2026-07-31-phase-2-backend-rds.md` | 12, 15, 16, 20 | `test_latency_includes_the_persistence_component`, `test_p95_latency_under_budget` |
| H29 | High | `2026-07-31-phase-5-deploy-docs.md` | 17, 18 | `test_aws_down_records_the_auto_restart_deadline`, `test_db_restore_round_trips_a_dump` |
| H30 | High | `2026-07-31-phase-2-backend-rds.md` | 11, 12, 16 | `test_predict_stays_available_when_the_database_is_down` |
| H31 | High | `…phase-1-…` 12, 16; `…phase-5-…` 21 | P1 12, 16; P5 21 | `test_model_card_fairness_section_matches_the_measured_slices` |
| H32 | High | `2026-07-31-phase-5-deploy-docs.md` | 1 | `test_readme_shows_a_runnable_predict_example` |
| H33 | High | `2026-07-31-phase-5-deploy-docs.md` | 22 | `test_every_practice_row_has_a_status_and_evidence` |
| H34 | High | `2026-07-31-phase-5-deploy-docs.md` | 25 | `test_every_rubric_clause_has_an_owner_and_evidence` |
| H35 | High | `…phase-4-…` 6–10; `…phase-5-…` 13 | P4 6–10; P5 13 | `test_every_third_party_action_is_pinned_to_a_full_commit_sha`, `test_every_suppression_has_a_reason_and_an_unexpired_date` |
| H36 | High | `…phase-4-…` 7, 8; `…phase-a2-…` 19 | P4 7, 8; A2 19 | `test_ci_never_runs_terraform_plan`, the `gha-ci` role is not created at all |

### Named, unnumbered findings

| Id | Owning plan file | Task | Test that fails if unfixed |
|---|---|---|---|
| held-out test-set discipline (§6.1) | `2026-07-31-phase-1-train-register.md` | 11, 16 | `test_second_touch_of_the_same_data_version_is_refused`, `test_the_guard_survives_a_fresh_interpreter` |
| fairness (§6.2) | `2026-07-31-phase-1-train-register.md` | 12, 16 | per-identity-term FPR with bootstrap intervals; low-power groups reported, not dropped |
| skops static allowlist (REG-6.3d) | `2026-07-31-phase-2-backend-rds.md` | 6 | `test_trusted_types_is_a_literal_tuple_of_strings`, `test_type_outside_the_allowlist_is_rejected` |
| digest provenance (TAIL-1) | `…phase-2-…` 5, 6; `…phase-5-…` 9, **10a** | P2 5, 6; P5 9, 10a | `test_digest_of_record_comes_from_the_committed_model_card`, `test_sidecar_artifacts_are_digest_verified_against_the_model_card` |
| XSS / verbatim render (§6.3) | `2026-07-31-phase-3-ui-monitoring-rescorer.md` | 9 | AST scan for `unsafe_allow_html`, `st.markdown(`, `st.write(`, `st.html(` |
| **IFACE-DB-SCHEMA** | `2026-07-31-phase-2-backend-rds.md` | **10a** | `test_the_review_queue_sampling_column_has_exactly_one_name`, `test_the_schema_entry_point_has_the_name_phase_3_imports` |
| **DRIFT-ARTIFACTS** | `2026-07-31-phase-5-deploy-docs.md` | **10a** | `test_every_artifact_path_written_into_an_env_file_is_actually_fetched` |
| **SCHEMA-PROD** | `2026-07-31-phase-5-deploy-docs.md` | **19a** | `test_aws_up_applies_the_full_schema_before_it_verifies_health`, `test_the_deployed_database_carries_every_phase3_column` |
| **Terraform duplicate declaration** | `2026-07-31-phase-a2-terraform.md` | **5a** (Phase 3 Task 12 reduced) | `test_no_security_group_or_variable_is_declared_twice_in_the_root_module`, `test_the_root_module_actually_validates` |

---

## 2. Rubric clauses

Every clause below is parsed **out of `docs/week9_FinalProject.md` itself** by `rubric_clauses()` in `tests/unit/test_rubric_matrix.py` (Phase 5 Task 25), so the matrix cannot drift from the document being graded. `test_every_rubric_clause_has_an_owner_and_evidence` fails on any clause with no row, an empty owner, an empty evidence cell, a `FAIL` verdict, an unjustified `PARTIAL`, or a backticked evidence path that does not exist on disk.

| Clause | Owning plan task | Evidence artifact |
|---|---|---|
| **Core 1** Experiment tracking & registry | P1 14, **14a**, 15 | W&B run pages; public Registry page verified logged out |
| **Core 2** ML model backend | P2 15, 18 | `/predict` and `/health` on EC2 #1 |
| **Core 3** Persistent data store | P2 10, **10a**; P5 **19a** | RDS Postgres 16, private; `tests/integration/test_deployed_schema.py` |
| **Core 4** Frontend interface | P3 10; P5 12 | Streamlit user UI on EC2 #2 |
| **Core 5** Model monitoring dashboard | P3 13–16, **16a**; P5 **20b** | Dashboard on EC2 #3; `docs/evidence/p5-seed-demo-production.md` |
| **Core 6** CI/CD pipeline | P4 8, 12, 13 | `.github/workflows/ci.yml`; blocked-merge screenshot |
| **1.1** Model development | P1 2, 3 | `model/train_classical.py`; baseline metrics in the run |
| **1.2** Experiment tracking (git commit, hyperparameters, metrics incl. accuracy, data version**s**) | P1 9, 14, **14a** | Run config carries `git_sha`, `seed`, `raw_sha256`, `split_version`, `env_version`, `data_version`, hyperparameters, thresholds |
| **1.3** Model versioning & registry, visible promotion | P1 15 | `check_public_registry` — unauthenticated GraphQL read, no `Authorization` header |
| **2.1** FastAPI backend, `/predict` + `/health`, loads a specific version | P2 6, 15, 18 | `tests/integration/test_deployed_traversal.py` |
| **2.2** Cloud database (managed AWS) | P2 10, **10a**; A2 9 | `aws_db_instance.main`, private, gp2, final snapshot |
| **2.2-log-every-request** (sub-clause) | P2 12, 15 | `test_failed_prediction_still_writes_a_row` |
| **3.1** User interface | P3 10 | Streamlit UI; two-click agree/disagree control |
| **3.2** Monitoring dashboard | P3 13–16 | `monitoring/dashboard.py` |
| **3.2-different-server** (sub-clause) | P5 12, 20 | `test_the_monitoring_instance_is_a_different_host_from_the_frontend` |
| **3.2-not-json-files** (sub-clause) | P3 **16a** | `test_the_dashboard_reads_no_prediction_data_from_a_file` |
| **3.2-latency** (sub-clause) | P3 13; P5 **20b** | `test_production_latency_series_spans_at_least_seven_buckets` |
| **3.2-target-drift** (sub-clause) | P1 13; P3 14; P5 **10a** | `test_production_drift_panel_has_a_reference_and_a_series` |
| **3.2-user-feedback** (sub-clause) | P3 6, 8, 10, 15 | `test_user_feedback_writes_a_user_sourced_row`; Horvitz-Thompson live accuracy |
| **4.1** Comprehensive testing | P4 4, 5 | Coverage floor 80 on `model`, `backend`, `monitoring` |
| **4.1-unit** (sub-clause) | P4 4 | `test_directory_layout_drives_the_markers` |
| **4.1-integration** (sub-clause) | P4 4, 5 | `test_integration_tests_cannot_silently_skip_in_ci` |
| **4.2** CI on PRs to `main`, linter + full suite, PRs cannot merge if checks fail | P4 8, 12, 13 | `test_administrators_cannot_bypass`; the blocked-merge screenshot |
| **5.1** Docker packaging | P2 21; P3 21; P5 3 | Four ECR repositories, immutable tags, digest-pinned bases |
| **5.2** AWS deployment to separate EC2 instances | P5 12, 20; A2 13 | Three EIPs answering `/health` |
| **5.3** README: setup, deployment, example user requests | P5 1, **1a** | `test_readme_shows_a_runnable_predict_example`, `test_readme_cost_agrees_with_the_cost_model` |
| **GitHub Repository URL** | P5 24 | `docs/submission-manifest.yml`, verified logged out |
| **Project Workflow Screenshots** | P5 24; **20b** | `docs/evidence/screenshots/`; density measured before the screenshot is taken |
| **Experiment Tracking Dashboard URL** | P5 24 | Public W&B project, verified logged out |

---

## 3. Cross-plan contradictions found and resolved

These are not coverage gaps. Each one would have made the suite unrunnable or the deploy impossible regardless of how well the findings were covered, and none was detectable by any single plan's tests because each suite parses only its own artifacts.

| # | Contradiction | Resolution | Guard |
|---|---|---|---|
| 1 | `model/contract.py::probs_to_dict` defined **three** times, three bodies, three error messages; Python keeps the last `def` | Phase 0 owns it; Phases 1 and 2 import and verify | `test_probs_to_dict_is_defined_exactly_once`, `test_the_canonical_adapter_raises_both_documented_messages` (P4 11) |
| 2 | The master plan's Interface Contracts block rewritten twice, incompatibly (7 fields vs 4; 68 rows vs 36) | Phase 0 Task 18's block is authoritative; P4 Task 11 verifies rather than rewrites; one conformance suite | `test_there_is_exactly_one_interface_contract_conformance_suite` (P4 11) |
| 3 | `[tool.pytest.ini_options] markers` rewritten three times; the last rewrite dropped `perf` while enabling `--strict-markers` | P4 Task 4 declares all four markers (`unit`, `integration`, `perf`, `awsapply`) and is the single declaration of record | `test_every_marker_used_in_the_tree_is_declared` (P4 4) |
| 4 | `MAX_INPUT_CHARS` = 5000 in `model/normalize.py` and 4000 in `backend/config.py`, both described as authoritative | `model/normalize.py` is the single source; `backend/config.py` re-exports | `test_the_input_cap_has_one_source_of_truth` (P2 4a) |
| 5 | Phase 3 and Phase A2 both declare `aws_security_group.{backend,frontend,monitoring,db}` and `variable "operator_cidrs"` in one root module | A2 is the Terraform scope of record; `app_ingress.tf` deleted; Phase 3 Task 12 reduced to `infra/exposure.py` | `test_no_security_group_or_variable_is_declared_twice_in_the_root_module`, `test_the_root_module_actually_validates` (A2 5a) |
| 6 | `demo_ingress_cidrs` vs `demo_cidrs` for one toggle | `demo_cidrs` | `test_only_one_demo_toggle_variable_name_exists_repo_wide` (A2 5a) |
| 7 | Phase 2 `inclusion_probability` NOT NULL vs Phase 3 `sample_rate` nullable; `ck_review_source` rejects `'user-report'`; two `feedback` column sets; `init_schema` vs `init_db` | Phase 3's shape wins; Phase 2 Task 10a lands it and renames repo-wide | `test_no_module_in_the_repo_still_says_inclusion_probability` (P2 10a) |
| 8 | P4 Task 4's `CI=true and not TEST_DATABASE_URL -> UsageError` guard vs Phase 0's subprocess test asserting `returncode == 0` inside Actions | `ci.yml`'s unit job exports `TEST_DATABASE_URL` | `test_the_ci_database_guard_does_not_fire_on_the_unit_job` (P4 4) |
| 9 | Phase 1's "Corrections to the master plan" table states the pre-v2 single-field `data_version` semantics | Row rewritten in place to name the three-field split (P1 Task 14a's amendment list) | `test_the_contracts_block_carries_no_superseded_text` (P4 11) |
| 10 | `docs/…/2026-07-30-delivery-plan-design.md:85` still asserts `$0.101/hr`, which A2 Task 16 spent a document refuting | Edited at source, per remediation 0.2 | `test_the_delivery_spec_no_longer_carries_the_superseded_hourly_figure` (P5 1a) |

---

## 4. Still uncovered, and why accepting it is defensible

Four items remain open. Each is named here rather than left to be discovered, and each has a stated reason and a re-open condition.

**1. C10 — the plans themselves are unmerged.** All eight phase plans plus the owner-decisions commit live only on `docs/owner-decisions-and-plans`. The premortem's C10 was "work that exists only on an unmerged branch is work that does not exist", and this is the same shape one layer up. It is not a *plan* defect and no test inside a plan can catch it. **Accepting is defensible only until implementation starts**; merge the branch first. There is nothing to test — the remedy is one `git merge`.

**2. Fold coverage is asserted on the fixture, not on the real 159,571-row corpus.** `model/data/run.py` prints `train=… test=… folds=…` and asserts nothing, so the delivery-spec constraint "every label including `threat` appears in every fold and in the test set" is enforced only against the 68-row synthetic fixture. Defensible because the fixture was constructed with real slack precisely to exercise the constraint, the split is seed-parametrized over five seeds, and `threat`'s real prevalence (~0.3%) at 5 folds gives a comfortable margin. **Cheap fix if it is ever wrong:** add `assert_fold_coverage(bundle)` to `run.py`, raising on any label with zero positives in any validation fold or in `test_df`, with a unit test that a doctored bundle raises. Re-open if the corpus is ever subsampled or a label is added.

**3. The once-only test-set ledger is keyed on the composite `data_version`, which includes `env_version`.** A numpy or scikit-learn bump therefore legitimately re-opens the held-out test set on an unchanged split. Defensible because a different library version *is* a different measurement, and pretending otherwise would let a silent numerical change ride on an old evaluation. It is now named in the header of `docs/test-set-touch-log.md` (Phase 1 Task 14a's amendment list) so it is a documented property rather than a surprise.

**4. Rubric 2.2's "It **may** also cache predictions" has no owner.** The rubric marks it optional in its own text. No cache is built, and building one would add a consistency surface with no graded return. `rubric_clauses()` does not parse it as a clause, and the matrix does not claim it.

**One residual risk that is covered but worth restating.** Task 24a's post-demo closure is a *procedural* gate: `test_every_control_is_recorded_closed_before_submission` fails while a control is open, and it is wired into the Phase 5 Task 26 phase gate, but the manifest dates are typed by a human. What makes them true is `scripts/close_demo.sh`'s final loop, which curls each endpoint from a host that is now off the allowlist and exits non-zero if any still answers. A date without that script's output in the evidence field is a date to distrust.
