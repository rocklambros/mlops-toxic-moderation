# Adversarial Premortem: Delivery Plan and Committed Specs

- Artifact under review: `docs/superpowers/specs/2026-07-30-delivery-plan-design.md` @ `f2fa374`, plus the design spec v1.1, the AWS foundation spec v1.0, the master roadmap, and the hardened Phase 0 plan
- Emphasis: conformance to `docs/week9_FinalProject.md`
- Date: 2026-07-30
- Perspectives: Red Teamer, Data Scientist, ML Engineer, Security Architect, MLOps/SRE, Governance/Risk. None skipped.
- Raw findings: 94. Surviving at Plausible or above after merge and cross-attack: 47.

**Protocol note.** The skill specifies five sequential rounds. This run used one parallel wave of six perspectives, each scoped to its own lead layer, because the artifact is a plan rather than a running system and the layers are separable in text. Convergence was judged across the merged set rather than round-over-round. Logged as a deviation.

**Method note that changes how these should be read.** Three perspectives did not merely read the Phase 0 plan — they materialised its code blocks and executed them under the pinned dependency set. Findings marked **[EXECUTED]** are reproduced observations, not inference. Two of them are reproductions of the *plan's own tests failing on the plan's own fixture*.

---

## The headline

The plan is strong on rigor and weak on three things it cannot see from inside itself: **its own code has never been run**, **its normative security register is unscheduled and untested**, and **its coverage matrix validates the plan against the design rather than against the rubric that assigns the grade**.

The realistic worst case is not failure. It is a working, genuinely deployed system that loses points on rubric 1.3, 3.2, 4.2, 5.2, and 5.3 because no checklist ever mapped the artifact to the thing being graded.

---

## Critical findings

### C1. The MinHash near-duplicate firewall detects ~29% of what it claims, and the plan's own test fails on its own fixture **[EXECUTED]**

- **Impact:** Critical · **Confidence:** Very likely (prior Very likely from two perspectives; raised on genuinely independent execution by both, not on count)
- **Evidence:** `phase-0:558` `dedup(df, threshold=0.9, num_perm=64)`. Executed: `MinHashLSH(threshold=0.9, num_perm=64)` resolves to **b=3, r=21**. P(detect) = 1−(1−J²¹)³ = **29.4% at J=0.90**, 46.0% at J=0.923. The planted near-duplicate `"you are an idiot!"` (J=0.923) survives. `assert len(out) == len(df) - 4` fails as `assert 33 == 32`.
- **Failure mode:** Phase 0 is red on day 1. The tempting repair is to relax the assertion, which converts a detected bug into a permanent silent one. On real Jigsaw, most true near-duplicates at the documented threshold pass into the split and inflate the held-out score — the exact contamination the design calls a hard requirement.
- **Cross-attack:** The Data Scientist's own counterargument is that Jigsaw's duplicate population may be bimodal (exact reposts, caught by pass 1; plus mostly-distinct comments), leaving the 0.85–0.98 band nearly empty and the leakage immaterial. **Sustained but reframed:** the premise is unmeasured in both directions, so the firewall's *necessity* and its *effectiveness* are both unknown — and C2 guarantees nothing downstream will ever measure either.
- **Fix:** `num_perm=128`, `threshold≈0.8` as a blocking stage, then **verify candidates with exact shingle-set Jaccard** before collapsing. Add a test asserting `1-(1-J**r)**b >= 0.99` at the operating point.

### C2. `assert_no_leakage` is a tautology — all three assertions pass by construction

- **Impact:** High → **Critical** in combination with C1 · **Confidence:** Very likely
- **Evidence:** `firewall_check.py` imports the same `_minhash` from `dedup` (`phase-0:1125`) with the same `threshold=0.9, num_perm=64` (`:1130`); `datasketch.MinHash` uses a fixed default seed, so band hashes are byte-identical. LSH banding is symmetric: a pair dedup failed to bucket cannot be bucketed by the gate.
- **Failure mode:** The gate the delivery spec made normative — "no MinHash near-duplicate leak" — has **zero detection power**. It ships green, providing documented, defensible-looking assurance for a property never checked.
- **Fix:** Genuine independence: exact Jaccard over a blocked candidate set at a **lower** threshold than dedup used (dedup 0.80, gate 0.70), reporting count and max cross-split Jaccard.

### C3. `solver='saga'` does not converge and is ~220× slower than the alternatives **[EXECUTED]**

- **Impact:** Critical · **Confidence:** Very likely (measured twice, two corpus sizes, on this Jetson)
- **Evidence:** delivery-spec §6.2 mandates `solver='saga'`. Measured on word 1-2 + `char_wb` 3-5: `saga` 493.5 s at n=15,000, hitting `max_iter=1000` **without converging**; `liblinear` 5.7 s converged in 6 iterations; `lbfgs` 2.2 s in 9. Extrapolated to 6 labels × 5 folds at 135k rows: **~37 hours** versus ~10–26 minutes.
- **Failure mode:** Phase 1's two-day window becomes an overnight run producing a `ConvergenceWarning`-tainted Production artifact, or burns the entire buffer on misdiagnosis. This is a defect **introduced by the hardening recovery**, not inherited.
- **Cross-attack:** The benchmark ran uncapped vectorizers while §6.2 mandates a `max_features` cap in the same sentence. **Partially sustained:** the cap reduces magnitude but cannot close a 220× gap, and non-convergence is a correctness failure independent of the cap. Compounded by C11 — no `max_features` value exists anywhere.
- **Fix:** `solver='liblinear'` (or `lbfgs`), with a convergence assertion in the test suite.

### C4. `CalibratedClassifierCV(OneVsRestClassifier(...))` raises `ValueError` on multi-label targets **[EXECUTED]**

- **Impact:** Critical · **Confidence:** Very likely (reproduced on the pinned sklearn 1.5.2)
- **Evidence:** delivery-spec §6.2 "Wrap the classifier in `CalibratedClassifierCV`". Executed: outer wrap → `ValueError: y should be a 1d array, got an array of shape (96, 6)` because `CalibratedClassifierCV.fit` calls `LabelEncoder().fit(y)`. Inner wrap `OneVsRestClassifier(CalibratedClassifierCV(LogisticRegression(...), cv=3))` fits and returns `(n, 6)`.
- **Failure mode:** The literal reading of a normative instruction is a hard crash. The 2 a.m. repair is to drop calibration — which silently voids the output contract's central promise, since the policy thresholds probabilities that then mean nothing.
- **Fix:** Specify the inner nesting explicitly. `method='sigmoid'` for rare labels (`threat` has ~406 train positives; isotonic on ~80 per-fold positives overfits). Calibration folds must be disjoint from threshold-tuning folds.

### C5. The monitoring dashboard — the most-emphasised graded requirement — has no data source

- **Impact:** Critical · **Confidence:** Very likely (two perspectives, genuinely different evidence: volume, and division-by-zero)
- **Evidence:** Rubric 3.2 requires latency over time, predicted-class distribution as drift, and live accuracy. The only volume any plan specifies is a **single** traversal (delivery-spec §3.3, master-plan:336). No task, gate, or deliverable creates prediction volume.
- **Failure mode:** A few dozen hand-typed rows in one session. "Latency over time" is a scatter across four minutes. "Drift" over one time bucket is a single bar. With `threat` under 0.3%, per-label precision/recall denominators are **zero**, so the panel renders `NaN` or a `ZeroDivisionError` traceback in the screenshot of the highest-weighted requirement.
- **Cross-attack:** A grader cannot distinguish thirty points from thirty thousand in a screenshot. **Partially sustained:** it holds for the latency panel and fails for the other two, because live accuracy is a *ratio* with zero denominators and drift needs ≥2 buckets.
- **Fix:** `make seed-demo` replaying ~2,000 held-out Jigsaw comments through `/predict` with back-dated `ts` spread over 7–14 days. Their labels are known, so `feedback` ground truth is free. Exit criterion: ≥N points across ≥7 buckets, live accuracy over ≥200 reviewed items.

### C6. No egress rule is specified anywhere, and Terraform deletes the default allow-all

- **Impact:** Critical · **Confidence:** Likely
- **Evidence:** foundation §7.1 specifies ingress only. `grep -r egress` across all specs returns zero. `resource "aws_security_group"` declared without an `egress` block removes the default `0.0.0.0/0`.
- **Failure mode:** Instance boots, `dnf install docker` hangs, the SSM Agent never reaches `ssm`/`ssmmessages`/`ec2messages` on 443, so the instance **never appears in SSM inventory**. There is no SSH by design, no bastion, no NAT, and no documented serial-console path. The sole remaining channel is the thing that is broken.
- **Fix:** Explicit egress 443, plus a written no-SSH debug runbook naming `aws ec2 get-console-output`, `aws ssm describe-instance-information`, and EC2 Serial Console.

### C7. `terraform apply` runs unattended on every push to `main` against an auto-resolving AMI

- **Impact:** Critical · **Confidence:** Likely
- **Evidence:** foundation §8 `deploy.yml` on push to `main` "runs `terraform apply`". §7.2 resolves the AMI from the SSM public parameter "so the image stays current without a code change".
- **Failure mode:** A README typo fix on day 14 coincides with an AL2023 republication. The `ami` attribute forces replacement of **all three instances**. Baked model artifacts, compose files, and pulled images are destroyed; the SSM roll fires against instances not yet registered. The `production` gate was self-approved for a docs change. The live URL dies and there is no SSH.
- **Fix:** Any one of: pin the AMI to a committed variable; `lifecycle { ignore_changes = [ami] }`; or split apply into a manually-dispatched workflow. Plus `paths-ignore` on `docs/**` and `**.md`.

### C8. The cut-line is structurally incapable of firing in time

- **Impact:** Critical · **Confidence:** Very likely (arithmetic on the plan's own two tables)
- **Evidence:** delivery-spec §8 "If day 15 arrives without a live stack, cut from the top." All four cut items are Slice 3 (§3.2), scheduled days **16–19** — *after* the trigger.
- **Failure mode:** Cutting on day 15 recovers **zero days**, because nothing on the list was ever on the days 1–14 critical path. Worse, cutting DistilBERT late means the fine-tune, ONNX export, and re-scorer image are already paid for, *and* it triggers a Terraform change (EC2 #3 drops to `t4g.small`) requiring an SCP edit and a re-apply on day 15.
- **Fix:** Move the trigger to a leading indicator — "if the day-9 smoke deploy has not succeeded by day 11, DistilBERT is cut immediately" — add day-8 and day-11 checkpoints with pre-committed reductions that are genuinely on the critical path, and move the rollback runbook off the cut list.

### C9. The rubric-to-artifact map does not exist; the coverage matrix validates the plan against the design

- **Impact:** Critical · **Confidence:** Very likely
- **Evidence:** master-plan:391-413 "Self-Review: Spec Coverage Matrix" — all 18 rows key on sections of the *design spec*. Not one row cites `docs/week9_FinalProject.md`.
- **Failure mode:** Four rubric clauses have **no owning task**: 4.2 "PRs cannot merge if checks fail", 5.3 "example user requests", 1.3 visible Staging/Production promotion, and 3.2's word "**user**" feedback. These are exactly the clauses that reward no design thinking and are therefore never surfaced by design reasoning.
- **Cross-attack:** The design *was* written from the rubric and cites it eight times. **Does not survive:** the citations cluster on the clauses the author found interesting; the four survivors are the boring ones. A matrix substitutes for remembering, not for judgment.
- **Fix:** Transcribe the rubric line-by-line into a matrix keyed on rubric clauses, with an owning task and an evidence artifact per row. One hour.

### C10. The delivery spec is itself on an unmerged branch — the failure it was written to fix is recurring

- **Impact:** Critical · **Confidence:** Very likely (verified: absent from `origin/main`)
- **Evidence:** The spec's §1 diagnoses that `097d558` "is **not an ancestor of `main`**". The spec itself was, at review time, likewise not on `main`. Its "verified" starting-position table cites `git ls-files` — a **branch-local** command — to support "the repository contains no code", while `git ls-tree -r docs/premortem-plan-hardening` returns a 146-line `infra/Makefile`. Three later commits on that branch (646 lines) are never dispositioned.
- **Fix:** Merge to `main`. Disposition or delete the third branch. Make `main` the only thing anyone reads.

### C11. Three static-credential claims are false, and one is verified on disk

- **Impact:** Critical · **Confidence:** Very likely
- **Evidence:** "No static AWS access key exists anywhere in this project" (foundation §4.2). Refuted three ways: (a) `OrganizationAccountAccessRole` trusts `arn:aws:iam::<mgmt>:root`, and two legacy IAM users with static keys live there, unconstrainable by SCP *by the same property the design celebrates*; (b) `~/.aws/sso/cache/*.json` on the Jetson holds an `accessToken` **and a `refreshToken`** under a client registration valid to 2026-10-29 — a portable 90-day credential to `AdministratorAccess` on the management account; (c) `~/.netrc` contains `machine api.wandb.ai` in plaintext, contradicting delivery-spec §2's "nothing is written to disk".
- **Failure mode:** Phase 0's first `make venv` runs an **unhashed** `pip install` on that box (the hashed lock is specified for "before Phase 1"). One malicious transitive dependency harvests the SSO refresh token, the W&B key, the Kaggle token, and the RunPod key in a single post-install hook — and the blast radius is the organisation, not the sandbox.
- **Fix:** `--require-hashes` from day 1 or install in a container; scope the permission set below `AdministratorAccess`; `aws sso logout` when idle; correct the three claims.

---

## High findings, condensed

| # | Finding | Impact · Confidence |
|---|---|---|
| H1 | `hits[0]` indexes an unordered `set`; which representative absorbs OR'd labels varies with `PYTHONHASHSEED`, so `data_version` is environment-dependent. CI runs bare `pytest`, not `make test`, so the Makefile's pin does not apply. **[EXECUTED]** | High · Very likely |
| H2 | The AWS foundation spec §7.2 — the Terraform scope of record, which the master plan says "**Read it before starting**" — still specifies **two** EC2 instances. The three-instance decision lives in one paragraph of a document the implementer is never told to open. Directly on rubric 5.1/5.2/3.2 | High · Very likely |
| H3 | The SCP instance-type allowlist is never enumerated in any artifact. `t4g.small` and the sanctioned upsize target `c7g.xlarge` are absent from the only sizing table the Terraform author reads. Authored day 1, fails day 9–11 as an opaque `UnauthorizedOperation` | High · Very likely |
| H4 | The `gha-deploy` OIDC trust policy as described in prose is the canonical OR-bug: `sub` as a two-element array is evaluated as OR, so any workflow declaring `environment: production` on **any** branch satisfies it, bypassing the required-review gate. `gha-deploy` also needs `iam:*` to apply `iam.tf`, and the SCP denies none of the role-based escalation verbs | Critical · Very likely |
| H5 | SSM `SendCommand` is fire-and-forget in every description. A tag match of **zero** instances returns a `CommandId` and exits 0, so the deploy job goes green while nothing was deployed | High · Very likely |
| H6 | `terraform destroy` — cost control #2 — will fail on `aws_db_instance` without `skip_final_snapshot` or a `final_snapshot_identifier`, leaving a half-destroyed billing stack. Setting `skip_final_snapshot = true` instead **permanently deletes the graded dashboard dataset** on every teardown | High · Likely |
| H7 | The `$0.101/hr` cost model counts four on-demand rates and nothing else: no EIPs (~$0.015/hr, a 15% understatement before anything else), EBS, RDS storage and backups, CloudTrail S3, GuardDuty, ECR, **Secrets Manager at ~$0.40/secret/month × 3**, CloudWatch, SNS, or data transfer | High · Very likely |
| H8 | Live accuracy pools a 100%-sampled flagged stratum with a p-sampled random-audit stratum and reports the ratio unweighted. Stratified collection without stratified estimation is still biased. No sampling rate is specified anywhere, so at demo volume the audit stratum is empty and the "correction" silently degrades to the bias it was introduced to fix | High · Likely |
| H9 | Rubric 3.2 says "**user** feedback"; the design collects only *reviewer* feedback, and the reviewer is the developer. Either resolution is bad: ship as designed and 3.2 is arguably unmet; bolt on a user control late and the graded metric becomes writable by any anonymous visitor | High · Likely |
| H10 | Rubric 4.2's "PRs cannot merge if checks fail" is a GitHub **setting**, not code. Nothing configures it, nothing screenshots it, and the solo developer is the admin who can bypass it unless "Do not allow bypassing" is explicitly ticked | High · Very likely |
| H11 | Rubric 1.3 requires a *visible* Staging/Production promotion. The submission checklist verifies only that the W&B **project** is public — a different surface from the **Registry** | High · Likely |
| H12 | Opening ingress for the demo also exposes the **reviewer UI**, on the same host and port (8501), behind one shared secret, with the frontend holding direct RDS write access to the graded metric | Critical · Likely |
| H13 | The rubric *mandates* a public W&B dashboard, and Phase 1 logs the skops artifact and `thresholds.json` there. That publishes the exact coefficient vector and decision boundary of a linear model — full white-box evasion, zero queries, undetectable. §6.3 hides the digest from `/health` while the deliverable hands out the model | Critical · Likely |
| H14 | Every `/predict` response returns `model_version` containing the digest that §6.3 goes out of its way to strip from `/health`. The control is inert | Medium · Very likely |
| H15 | No TLS anywhere. No 443, no ACM, no ALB, no reverse proxy in any Terraform file list — yet delivery-spec §4 asserts the frontend "calls the backend over **HTTPS**". The reviewer shared secret crosses the internet in cleartext | High · Very likely |
| H16 | One security group, one instance role, one DB user across three instances. A Streamlit RCE on the internet-facing box yields the W&B key, the reviewer secret, and master-user read/write on all three tables. No read-only role for the dashboard, which only ever `SELECT`s | High · Very likely |
| H17 | Detective controls are incomplete: `guardduty:UpdateDetector` (disable without delete), `cloudtrail:UpdateTrail`, `cloudtrail:PutEventSelectors`, and unrestricted `s3:DeleteObject` / lifecycle on the trail bucket are all undenied. No log-file validation, no Object Lock | High · Very likely |
| H18 | The two RDS "require" SCP statements repeat the exact key-absence class of trap the spec congratulates itself for catching. `Bool` on an absent key fails **open**; `BoolIfExists` fails **closed**, denying every `CreateDBInstance` — the `rds:DatabaseClass` outcome §5.1 spent a paragraph avoiding | High · Likely |
| H19 | The SCP does not deny what §5.1 claims. `ModifyInstanceAttribute` (the plan's own documented resize path) reaches any instance type without `RunInstances`; `CreateFleet` / `RequestSpotInstances` are separate undenied actions; service-linked roles are exempt from SCPs entirely; and `us-east-1` is allowed wholesale with only EC2 and RDS constrained | High · Very likely |
| H20 | `make data` on real Jigsaw takes ~30–33 min (measured 5.6–6.2 ms per MinHash on this box), and the exit gate runs it twice. The Makefile is hardcoded to the 36-row fixture, and **no task opens the real CSV before day 3**, so a schema mismatch from a third-party Kaggle mirror surfaces inside the training window | High · Very likely |
| H21 | The fixture has **zero slack**: three labels have exactly 6 positives, the 15% test split consumes one each, leaving 5 for 5 folds. Executed: `insult` fails the every-label-in-fold assertion at seed 7. Seed 42 passes by luck, and the tempting repair is seed-shopping — which is p-hacking the split | High · Very likely |
| H22 | The output contract enforces label-key membership and nothing else. Executed: it accepts `prob=-5.0`, `prob=42.0`, `latency_ms=-7`, and `severe_toxic=0.99` with `toxic=0.01` — the incoherence §6.2 declares must never be returned | High · Very likely |
| H23 | The "single authoritative array→dict adapter" §6.2 mandates has no name, no signature, and no file anywhere. Three call sites will `zip(LABELS, row)` independently, and the order-blind validator makes a transposition invisible to every test | High · Very likely |
| H24 | The master plan's Interface Contracts block — declared "authoritative" — still carries pre-hardening semantics (`data_version` as "sha256 over sorted deduped ids"), and drifts from the Phase 0 code in five places. The hardening commit never updated it | High · Very likely |
| H25 | The serving normalizer is specified as a superset of the dedup normalizer (adding homoglyph folding), but they are the same function. Closing the gap retroactively changes `dedup` output → changes `data_version` → moves the locked test set after models were registered. Not closing it means train/serve skew. §12 elsewhere concedes homoglyphs defeat normalization — a direct self-contradiction | High · Likely |
| H26 | Days 13–14 is the most wrong row: the first moment anything runs on EC2, with ECR auth, arm64 boot, digest-verified fetch against a fail-closed loader, instance-to-instance HTTP through a security group that documents only operator ingress, RDS connectivity, EIP association, and the SSM roll all first-time-ever simultaneously | High · Likely |
| H27 | No system observability. No container logs leave the box (no log driver configured anywhere), and the only alarm in the entire design is for root sign-in. Nothing pages when `/predict` is down — which §10 makes a *designed* behaviour whenever RDS is unreachable | High · Very likely |
| H28 | No latency budget, no percentile, no load test. `latency_ms` is stamped before persistence in the master plan's own ordering, so the graded chart omits the slowest component; and 503s write no row, so the slowest requests are structurally absent from the series | High · Very likely |
| H29 | The 7-day RDS auto-restart will fire during a 19-day project with a stop-between-sessions model. The documented remedy — "destroy rather than stop" — deletes the graded dashboard dataset. The two documented behaviours are mutually exclusive and no document notices | High · Very likely |
| H30 | `/predict` returning 503 on persistence failure hands an attacker an off switch: with no rate limit and a `db.t4g.micro`, modest concurrent traffic exhausts connections and the moderation endpoint is *down*, not degraded, for as long as the pressure lasts | High · Likely |
| H31 | Zero bias or fairness measurement, for a content-moderation classifier trained on Jigsaw — whose best-documented failure is over-flagging comments that merely *mention* identity groups. `auditing-model-fairness` is available and unlisted. `SECURITY.md` already cites a `MODEL_CARD.md` that does not exist | High · Very likely |
| H32 | The README is graded (5.3), is currently a placeholder on a public repo, is scheduled for day 15, omits "example user requests" from its owning task, and is **not** on the never-cut list | High · Likely |
| H33 | Every present-tense claim in the public `SECURITY.md` is false today (no code exists), and two are contradicted by the plan itself: "ingress restricted to a single operator address" versus a grader-reachable URL, and "holds no third-party user data" versus a public `/predict` | High · Very likely |
| H34 | The buffer (days 16–19, 21% of remaining schedule) is pre-allocated to work the plan itself certifies as ungraded. No day is allocated to a rubric self-grade or a dry-run submission | High · Very likely |
| H35 | Supply chain: unpinned third-party Actions (any of which can mint the `gha-deploy` OIDC token), no per-job `permissions:` block against a repo default of *write*, unpinned base images defeating SHA traceability, and ECR *basic* scan-on-push which cannot see Python dependencies and gates nothing | High · Very likely |
| H36 | `terraform plan` on pull requests through `gha-ci` is code execution on attacker-supplied `.tf` (providers, `data "external"`, module sources). Not currently reachable by fork PRs, but `pull_request_target` and `workflow_run` both reintroduce it, and the rubric does not ask for a plan step at all | High · Likely |

---

## Dropped ledger

| Claim | Anchor | Posterior | Reason for drop |
|---|---|---|---|
| Prediction caching required for rubric 2.2 | rubric:44 "**may** also cache" | Remote | Rubric says "may"; explicitly optional |
| `DatasetBundle` frozen-dataclass `__eq__`/`__hash__` raise on DataFrames | `phase-0:1054` | Unlikely as a *defect* | Real, but no code path compares bundles; retained as a Phase 0 code-quality note |
| `model_version` pydantic protected-namespace warning | `phase-0:844` | Unlikely | Cosmetic; one-line `model_config` fix, no behavioural impact |
| Stated test counts wrong in two Phase 0 tasks | `phase-0:596`, `:1189` | Unlikely as a defect | Retained instead as **evidence** for the finding that the code was never executed |
| ECR keep-last-10 erodes rollback targets | foundation §7.2 | Plausible → kept | Not dropped; folded into H6/rollback remediation |
| Kaggle mirror could be re-uploaded silently | master-plan:435 | Plausible → kept | Folded into the `raw_sha256` provenance fix |

**Tail risks — Critical and irreversible, below Plausible, parked with triggers.**

- **Poisoned W&B artifact achieving RCE on EC2.** The digest and the artifact live in the same trust domain under one API key, and that key is deliberately shared with RunPod *community* pods. Currently Remote because no adversary is known to target this project. **Trigger that raises it:** any evidence of W&B credential exposure, or a decision to use community rather than secure-cloud pods. **Cheap pre-mitigation regardless:** record `MODEL_DIGEST` in the git-committed model card, which breaks the co-location for free.
- **Legacy management-account key compromise.** Two static keys of unknown age hold a path to `OrganizationAccountAccessRole`. Remote absent evidence of exposure. **Trigger:** the RCAP IAM audit returning key age > 1 year or an over-broad policy.

---

## Convergence

Not converged. One wave produced 47 surviving findings at Plausible or above, of which 11 are Critical. The stop signal (a round yielding no survivors above Plausible, or only Low impact) was not reached. A second wave is warranted **after** remediation, targeted at the remediated artifacts rather than at these same layers.

---

## Prioritised remediation

Ordered by expected cost reduction per unit of effort — impact weighted by posterior confidence, cheaper fixes breaking ties.

### Tier 0 — free, and everything else depends on them

| # | Change | Closes | Verification |
|---|---|---|---|
| 0.1 | Merge the delivery spec to `main`; disposition the third branch; make `main` the single source | C10 | `git cat-file -e origin/main:<spec>` |
| 0.2 | Edit AWS foundation §7.2 and master-plan Phase A task 4 / Phase 5 heading to three instances **directly** — do not rely on a supersession table | H2 | `grep -c "EC2 #3"` in the foundation spec |
| 0.3 | Enumerate the SCP instance-type allowlist as the union `t4g.small, t4g.medium, t4g.large, c7g.xlarge` in Phase A task 2 | H3 | Day-1 test: launch and terminate one of each |
| 0.4 | Transcribe the rubric into a clause-keyed matrix with an owning task and evidence artifact per row | C9, H10, H11, H32 | Every rubric clause has a non-empty owner |
| 0.5 | Rewrite the cut-line trigger to a day-9/day-11 leading indicator; add day-8 and day-11 checkpoints with critical-path reductions; move rollback off the cut list | C8, H34 | Checkpoints appear as schedule rows |
| 0.6 | Correct the three false static-credential claims and convert `SECURITY.md` to a Status column | C11, H33 | No present-tense claim without an implemented artifact |

### Tier 1 — one-line code changes with large blast radius

| # | Change | Closes |
|---|---|---|
| 1.1 | `solver='liblinear'`; assert convergence in tests | C3 |
| 1.2 | Invert to `OneVsRestClassifier(CalibratedClassifierCV(...))`; `method='sigmoid'` for rare labels; disjoint calibration and threshold folds | C4 |
| 1.3 | `num_perm=128`, `threshold=0.8`, **exact-Jaccard verification** of every LSH candidate before collapse | C1 |
| 1.4 | Rewrite `assert_no_leakage` to use exact Jaccard at a lower threshold than dedup | C2 |
| 1.5 | `rep = index_of[min(hits)]`; set `PYTHONHASHSEED=0` in pytest config plus a `conftest.py` guard | H1 |
| 1.6 | Raise rare-label fixture positives to ≥9; parametrize split tests over ≥5 seeds | H21 |
| 1.7 | Add `ge=0, le=1` on `prob`, `ge=0` on `latency_ms`, a `max_prob` consistency validator, and a hierarchy validator to `PredictionResponse` | H22 |
| 1.8 | Name the array→dict adapter in the Interface Contracts block: `probs_to_dict(row: np.ndarray) -> dict[str, float]` | H23 |
| 1.9 | Update the Interface Contracts block to match the hardened Phase 0 code in all five drifted places | H24 |

### Tier 2 — infrastructure defects that surface as day-13 outages

| # | Change | Closes |
|---|---|---|
| 2.1 | Explicit egress 443; no-SSH debug runbook | C6 |
| 2.2 | Pin the AMI; `paths-ignore` docs from `deploy.yml`; split apply from roll | C7 |
| 2.3 | Poll `GetCommandInvocation` to terminal state; assert invocation count equals expected; `curl /health` per EIP as the real gate | H5 |
| 2.4 | `skip_final_snapshot = false` with a timestamped identifier; `backup_retention_period >= 1`; `make db-dump` inside `aws-down` | H6, H29 |
| 2.5 | Pull a throwaway single-instance smoke deploy forward to **day 9** | H26 |
| 2.6 | Split `sub` into a single-valued `StringEquals` plus `job_workflow_ref`; scope `gha-deploy`; drop `plan` from PR CI | H4, H36 |
| 2.7 | SHA-pin all Actions; add per-job `permissions:`; pin base images by digest | H35 |
| 2.8 | Rebuild the cost model with all eleven omitted line items; add a nightly EventBridge stop rule as a hard control | H7 |
| 2.9 | `awslogs` log driver to the existing log groups; one health alarm to the existing SNS topic | H27 |
| 2.10 | Deny `UpdateDetector`, `UpdateTrail`, `PutEventSelectors`; enable log-file validation; restrict trail-bucket deletes | H17 |
| 2.11 | Fix the two RDS SCP statements with the correct absence semantics; add `ModifyInstanceAttribute`, `CreateFleet`, `RequestSpotInstances` denies | H18, H19 |
| 2.12 | Per-tier security groups, instance roles, and a read-only DB role for the dashboard | H16 |
| 2.13 | TLS terminator, or an explicit documented decision to accept cleartext with the reviewer secret rotated after the demo | H15 |

### Tier 3 — rubric conformance and honesty

| # | Change | Closes |
|---|---|---|
| 3.1 | `make seed-demo` replaying ~2,000 held-out comments with back-dated timestamps; exit criteria on chart density | C5 |
| 3.2 | Add a user-facing agree/disagree control writing `feedback` with `source='user'` | H9 |
| 3.3 | Horvitz-Thompson weighting with a pinned `RANDOM_AUDIT_RATE` stored per row; display per-stratum n and a Wilson interval | H8 |
| 3.4 | Configure branch protection with "do not allow bypassing"; screenshot the blocked merge | H10 |
| 3.5 | Add registry-page evidence to the submission checklist | H11 |
| 3.6 | Log artifacts to a **private** W&B project; keep runs, metrics, `data_version`, and git SHA public | H13 |
| 3.7 | Opaque short version label on the public listener; digest to logs and the model card | H14 |
| 3.8 | Reviewer UI on its own port, not exposed by the demo toggle | H12 |
| 3.9 | Per-identity-term fairness slice of the held-out set; a fairness section in the model card | H31 |
| 3.10 | Write the README skeleton now, including `curl -X POST /predict` examples | H32 |
| 3.11 | Async persistence with a bounded durable buffer, or a flagged `logged=false` row, instead of 503 | H30 |
| 3.12 | Move the Kaggle download and `load_raw` smoke check to **day 1**; record `raw_sha256`; split `data_version` into `raw_sha256` / `split_version` / `env_version` | H20 |
| 3.13 | Cap pending-review age; purge `input_text_snapshot` on a hard TTL regardless of status | Retention |
| 3.14 | Email the instructor two questions: does the dashboard need its own EC2 separate from *both* backend and frontend, and does a public W&B project satisfy the Model Registry requirement | Interpretive risk on H2, H11 |

---

## Residual risk after remediation

Accepting the following is defensible; everything else above is scheduled for closure.

- **Phase A's opportunity cost.** The AWS Organizations foundation earns nothing the rubric asks for and is the highest-variance task in the schedule. It is retained because it is already largely built and because Identity Center is the only credential path that now exists. Mitigated by pre-authorising a fallback: if A2 is not green by end of day 11, provision EC2 and RDS by console and keep the Terraform as evidence for 5.2.
- **Solo oversight.** Every procedural gate resolves to one principal. The technical controls are real; the review controls are nominal. Partially mitigated by 3.14 and by this premortem, which is itself subject to the same limitation.
- **Adversarial evasion.** Cross-script homoglyphs and paraphrase defeat normalization. Named in the model card. The review queue does not mitigate it, because a successful evasion is never flagged.
- **The two parked tail risks** above, by reference.
