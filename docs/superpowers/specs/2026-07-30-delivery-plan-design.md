# Design Spec: Delivery Plan and Hardening Reconciliation

- Version: 1.0
- Owner: Rock Lambros
- Date: 2026-07-30
- Status: approved for planning
- Amends: `docs/2026-07-01-toxic-moderation-mlops-design.md` (v1.1), `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`
- Scope: how this project gets built and shipped in the time remaining, and which previously-orphaned hardening is now normative

## 1. Why this exists

Two problems, both discovered on 2026-07-30 by inspecting the repository rather than by reading its documentation.

**The plan lost a generation of review.** Commit `097d558`, "Harden toxic-moderation plan against the assignment rubric", rewrote all four planning documents with roughly 333 lines of remediations. It is **not an ancestor of `main`**. The branch that became the AWS account foundation forked from `c28e00f` and independently rewrote the same documents, so the newer branch by wall-clock time is the older one by review depth. Every fix in `097d558` was absent from what this project was about to build.

**The schedule is tighter than the plan's shape admits.** The assignment is due 2026-08-18. As of 2026-07-30 the repository contains zero code — `git ls-files` returns documentation only — across seven phases, of which one had a detailed plan, and that plan was the pre-hardening version. The reuse map points at a monorepo path (`/Users/klambros/github_projects/MLOPS-Comp-4450-1`) that does not exist on the build machine, so nothing is adapted and everything is written.

This spec resolves both. It makes the recovered hardening normative, reorders the work so the graded surface lands early, and states what gets cut if the schedule slips.

## 2. Starting position, verified

| Fact | Value | How verified |
|---|---|---|
| Days remaining | 19 (2026-07-30 → 2026-08-18) | Assignment front-matter `due_date` |
| Code in repository | **None.** Documentation only | `git ls-files` |
| Phases with a detailed plan | 1 of 7, and it was the stale copy | Directory listing of `docs/superpowers/plans/` |
| Monorepo for the reuse map | **Absent from this machine** | Filesystem search |
| Kaggle credentials | Present in `pass`, **live-probed HTTP 200** | Authenticated call to the Kaggle datasets API |
| W&B credentials | `pass wandb/api-key`, `~/.netrc` shows `api.wandb.ai` | `pass ls`, netrc inspection |
| RunPod credentials | `pass runpod/api-key` | `pass ls` |
| Jetson toolchain | Terraform 1.15.8, AWS CLI 2.36.3, both `~/.local/bin` | `terraform version`, `aws --version` |
| AWS Identity Center | Stage B complete, `rc-mgmt` gate passing | `aws sts get-caller-identity` |
| AWS account for the workload | **Does not exist yet** | `organizations list-accounts` returns one account |
| Repository visibility | Public, gitleaks-clean across 23 commits | `gitleaks detect` |

Every credential is sourced through `pass show` at the moment of use. Nothing is exported into a shell profile and nothing is written to disk, which keeps plaintext lifetime bounded to the process that needs it.

## 3. Sequencing

### 3.1 Phase A splits, and A1 runs first

The committed roadmap orders phases by dependency: Phase A precedes Phase 2 because Phase 2 needs RDS. That is correct but incomplete, because it schedules by what blocks what rather than by what has irreducible latency.

Account provisioning has a property code does not: **more effort on day 10 does not make it finish faster.** `organizations:CreateAccount` polls asynchronously. Establishing the root break-glass depends on password-recovery mail reaching `rock+aws-mlops-toxic@rockcyber.com`, and the AWS foundation spec §3 records that `rockcyber.com` routes through Mimecast, "whose recipient validation is a known cause of plus-addressed mail being rejected before it reaches the mailbox." If that address does not deliver, it must be discovered with eighteen days of runway, not eight.

Phase A therefore splits:

- **A1 — account provisioning.** `infra/aws/bootstrap.sh` and `infra/aws/scp-sandbox-guardrails.json`. Creates the OU, the SCP, the member account, the alternate contacts, the permission sets, and the Terraform state bucket. **Runs immediately.** An empty account holds no billable resources, so starting early costs nothing and de-risks the only task that can hard-block the deliverable.
- **A2 — infrastructure.** `infra/terraform/`. VPC, three EC2, RDS, ECR, IAM and OIDC roles, CloudTrail, GuardDuty, budget. Runs when the slice is ready to deploy, so nothing bills while the application is still being written locally.

### 3.2 Three slices

```
A1        bootstrap.sh                 account, SCP, state bucket        [~$0/mo]
SLICE 1   0 → 1-min → 2 → 3            full graded surface, local compose
SLICE 2   A2 → 4 → 5                   Terraform, CI gate, ECR + SSM, LIVE
SLICE 3   sweep · DistilBERT · ONNX · rescorer · AIBOM · SBOM · rollback
```

Slice 1 exercises all six graded components on the build machine with zero AWS spend and no RunPod dependency: the classical model is TF-IDF plus one-vs-rest logistic regression over roughly 160k short comments, which is CPU-minutes on this aarch64 box. Slice 2 makes it live. Everything in Slice 3 is severable.

### 3.3 Integration gate between phases

No phase is complete until **every route and integration it introduces is proven working against a real dependency**, not a mock. Unit tests may use fixtures; the phase gate may not. Concretely: Phase 2 is not done until `/predict` and `/health` answer against a real Postgres and a row lands in `predictions`; Phase 3 is not done until a submitted comment traverses predict → log → enqueue → review → feedback → dashboard; Phase 5 is not done until the same traversal succeeds against the deployed stack over the network.

## 4. Runtime topology

Three instances, which is the literal reading of the rubric rather than a permissive one. Rubric 5.1 names "one container for the FastAPI backend, one for the frontend"; 5.2 requires deployment "to separate EC2 instances"; 3.2 requires the monitoring dashboard on "a different EC2 server".

| Resource | Class | Runs | Notes |
|---|---|---|---|
| EC2 #1 | `t4g.medium` (2 vCPU / 4 GB) | FastAPI `/predict` `/health` | Memory bounded by the `max_features` cap on TF-IDF. Measure, do not assume |
| EC2 #2 | `t4g.small` (2 vCPU / 2 GB) | Streamlit user + reviewer UI | Thin client; calls the backend over HTTPS and reads RDS |
| EC2 #3 | `t4g.medium` (2 vCPU / 4 GB) | Monitoring dashboard, and the re-scorer if it lands | Drops to `t4g.small` if DistilBERT is cut; upsize only against measured ONNX throughput |
| RDS | `db.t4g.micro`, Postgres 16, 20 GB gp3, Single-AZ | Shared state | Private subnets, `manage_master_user_password = true` |

Roughly `$0.101/hr` with everything running, which is *cheaper* than the superseded two-instance design because EC2 #2 no longer has to be a `t4g.large` sized for a re-scorer that now sits behind a cut-line.

**The SCP instance-type allowlist must include `t4g.small`,** which the two-instance design never required. Omitting it denies the frontend launch. The allowlist deny must stay scoped to `Resource: "arn:aws:ec2:*:*:instance/*"` per the AWS foundation spec §5.1 trap, or it denies every launch including the intended ones.

## 5. The live-URL problem

The recovered hardening flagged this against the Learner Lab design. **It survives the move to a real account, because the mechanism is unchanged: an EC2 public IPv4 address is released on stop and a different one is assigned on start.** The current design has no Elastic IP, and the cost model explicitly instructs stopping instances between sessions. Those two facts together mean any URL captured during development is dead by the next session.

The rubric's Deliverables list asks for a repository URL, screenshots of "the working prototype running live on EC2", and a W&B dashboard URL. It does **not** name a live endpoint as a deliverable, so screenshots technically discharge the requirement. But the submission accepts a `website_url`, a grader may reasonably try it, and a dead link reads worse than no link.

**Resolution: allocate an Elastic IP per public-facing instance.** The cost analysis is more favourable than it first appears. Since 2024 AWS charges roughly `$0.005/hr` for *every* public IPv4 address, including auto-assigned ones, so an Elastic IP attached to a running instance costs exactly what the auto-assigned address already cost. The only marginal charge is for addresses held while their instance is stopped — three addresses at about `$11/month` worst case, against a `$100` ceiling, and far less because the stack is destroyed after grading.

This also removes a development annoyance: the security-group ingress allowlist and any bookmarked URL stop needing revision after every stop/start cycle.

## 6. Hardening register

Every item recovered from `097d558`, with its disposition under the current AWS design. Items marked **normative** bind the implementation.

### 6.1 Data and leakage — normative

| Item | Why it matters |
|---|---|
| Label-OR reconciliation across collapsed duplicate groups | Dropping a duplicate silently discards its labels; a `threat` positive can vanish with its copy, and `threat` is under 0.3% of the corpus |
| `data_version` hashes the realized split, a per-id label fingerprint, and pinned library versions | Hashing only surviving IDs collides silently when labels change or `iterative-stratification` is bumped |
| Firewall gate asserts three properties: no ID overlap, no exact-normalized text leak, **no MinHash near-duplicate leak** | The near-duplicate check is the class of contamination dedup exists to prevent, and the pre-hardening gate never verified it |
| Test fixture must exercise the MinHash-LSH branch | The old fixture's "near-duplicate" collapsed at the exact-normalized step, so the LSH code path was never executed by any test |
| The held-out test evaluates the single model already chosen by cross-validation; it never *chooses* between classical and DistilBERT | Picking the better of two test numbers is selection on the test set and biases the winner upward |

### 6.2 Modeling and serving — normative

| Item | Why it matters |
|---|---|
| Wrap the classifier in `CalibratedClassifierCV` fit on validation | The output contract promises calibrated probabilities and the policy thresholds them. A bare `class_weight='balanced'` logistic regression is **not** calibrated and inflates rare-label probabilities |
| Cap both vectorizers with `max_features`; `solver='saga'`, `max_iter` high enough to converge | Bounds EC2 #1 memory and prevents silent non-convergence on high-dimensional char features |
| Serving-path input normalization shares the dedup normalizer (NFKC, confusable/homoglyph folding, lowercase, whitespace collapse), plus a max-length cap | Trivial obfuscation otherwise bypasses the classifier. Residual cross-script and paraphrase evasion is a model-card limitation — the review queue is *not* a safety net, because a successful evasion is unflagged and never enqueued |
| Hierarchically coherent flags: `severe_toxic` implies `toxic` | The contract must never return "severe but not toxic" |
| A single authoritative array→dict adapter shared by the API, the re-scorer, and the DB layer | Independent `zip()` re-derivations mislabel probabilities silently if column order drifts |
| Accuracy is logged per run and shown on the dashboard, but is never a promotion or comparison metric | Rubric 1.2 and 3.2 name accuracy explicitly; the design bans it only as a *headline* metric |
| Confidence intervals use a stratified bootstrap that handles resamples containing no positives | `threat` has roughly 72 test positives; naive bootstrap crashes or silently drives a promote decision inside noise |
| DistilBERT uses `problem_type="multi_label_classification"` | Otherwise HF Trainer defaults to softmax cross-entropy on a six-column target and trains the wrong objective |

### 6.3 Security — normative

| Item | Why it matters |
|---|---|
| `skops.io.load` with an **explicit static** trusted-type allowlist | `get_untrusted_types()`-then-trust-all silently voids the control |
| The expected digest is recorded independently in the W&B immutable version alias, never derived from the artifact being loaded | SHA-256 proves integrity in transit, not provenance. Independent recording plus fail-closed is what actually closes the poisoned-artifact path |
| Streamlit never uses `unsafe_allow_html` for user content | Inputs are adversarial *by definition* here; stored XSS would steal the reviewer session |
| `reviewer_id` derived server-side from the authenticated session | A client-supplied reviewer identity is unauthenticated attribution |
| `/predict` input-size cap, rate limit, and demo API key or source allowlist | A public endpoint on a public repo with neither is free denial-of-service |
| `WANDB_API_KEY` reaches the build only through BuildKit secret mounts | A build-arg or `ENV` bakes it into an image layer permanently |
| Dependencies install from a hashed lock (`--require-hashes`) | The build machine holds live credentials |
| `/health` omits the full artifact digest on the public listener | Fingerprints the exact model for an attacker crafting evasions |
| RDS private, security groups scoped to the instances, least-privilege instance profiles, no static keys on any box | Already the AWS foundation design; restated because it must hold from first provision, not from the last phase |

### 6.4 Monitoring and workflow — normative

| Item | Why it matters |
|---|---|
| `review_queue.source` distinguishes `flagged` from `random-audit`, and feedback is computed over **both** | Otherwise live accuracy only ever measures the model's own flagged set and is structurally blind to confidently-allowed false negatives — the costly missed `threat`. This is the graded metric, and uncorrected it is misleading |
| `review_queue.input_text_snapshot` copies the comment at enqueue time | Review and re-scoring must not depend on `predictions.input_text`, which the retention purge nulls at 30 days |
| The retention purge exempts rows whose review is still pending | Otherwise the purge destroys the reviewer's evidence mid-workflow |
| `review_queue` is depth-capped and per-source rate-limited | A flood of toxic submissions would otherwise bury real items and poison the graded metric |
| Screenshots must not capture raw user text | The deliverable is a public artifact |

### 6.5 Superseded by the AWS foundation design — not ported

| Item | Disposition |
|---|---|
| `LabRole`, pasted per-session STS credentials, `us-east-1`, x86 `t3`, `vockey` | Dead with the Learner Lab |
| "Develop private, flip public at submission" | Already public as of 2026-07-30; the scrub and `SECURITY.md` are done |
| "The AWS Budget action stops instances rather than only alerting" | **Conflicts with a locked decision.** The AWS foundation spec §5.3 declines an automated stop action by owner decision, compensating with the SCP instance-type allowlist. Recorded as accepted residual risk in §12, not silently dropped |
| Elastic IP and bring-up runbook | **Not dead — re-adopted.** See §5 |

## 7. Schedule

| Days | Work | Gate |
|---|---|---|
| 1 | Reconcile docs; write and run `bootstrap.sh` (A1) | Account exists in the `Sandbox` OU, SCP attached, root break-glass established |
| 1–2 | Phase 0, hardened plan, TDD | `make data` twice yields an identical `data_version`; firewall gate passes and fails correctly on an injected leak |
| 3–4 | Phase 1 minimal: train, evaluate, calibrate, tune thresholds, register | Production artifact with digest; public W&B project showing git SHA, hyperparameters, `data_version`, metrics |
| 5–6 | Phase 2: FastAPI, safe loader, policy, RDS schema | `/predict` contract-valid against real Postgres; tampered artifact refuses to load |
| 7–8 | Phase 3: user UI, reviewer UI, monitoring dashboard, feedback | compose end-to-end: submit → predict → log → enqueue → review → feedback → dashboard |
| 9–11 | Phase A2: Terraform | `apply` and `destroy` both clean; a denied action observed to fail; no security group opens 22 |
| 12 | Phase 4: CI gate | A deliberately failing test demonstrably blocks merge |
| 13–14 | Phase 5: `deploy.yml`, ECR, SSM roll, digest-verified artifact fetch | **Live on EC2. All six graded components deployed** |
| 15 | README, model card, screenshots, submission checklist | Three deliverables in hand |
| 16–19 | Slice 3 depth, in cut-line order | Buffer |

Four days of buffer, and it should be expected to go. Phase A2 is the least trustworthy estimate: forty Terraform resources applied against a live SCP, where a guardrail defect surfaces as a confusing `apply` failure rather than a clean denial.

## 8. Cut-lines

Ordered. If day 15 arrives without a live stack, cut from the top.

1. AIBOM, SBOM, rollback document — ungraded, cheap to append later
2. W&B hyperparameter sweep on RunPod — one tracked comparison run satisfies "experiment tracking"
3. **DistilBERT entirely** — fine-tune, ONNX export, re-scorer worker, and EC2 #3's second container. The rubric never asks for a transformer; the design's rationale for it is rigor, not points
4. The reviewer's second-opinion column — the reviewer still labels, just without a challenger score

**Never cut:** the six graded components, the leakage firewall, safe model loading, the CI gate, or the three submission deliverables.

## 9. Testing

Test-driven throughout: write the failing test, run it and see it fail, write the minimal code, run it green, commit. One small commit per task.

| Layer | Approach |
|---|---|
| Phase 0 | Pure unit against a committed synthetic fixture. Determinism asserted by running twice and comparing `data_version`. The fixture must exercise the MinHash branch |
| Phase 1 | Pipeline-factory shape `(n, 6)`; TF-IDF provably inside the CV pipeline; threshold tuning never sees test; the once-only test guard actually guards; calibration measurably improves reliability |
| Phase 2 | Unit on policy boundaries and the fail-closed loader (tampered artifact refuses). Integration against real Postgres for the full round trip |
| Phase 3 | Integration: seeded queue drains; reviewer submit writes labels and derives feedback; dashboard aggregations return expected shapes |
| Phase 4 | Prove the gate by opening a PR with a failing test and observing the block |
| Phase 5 | `docker compose up` serves end-to-end locally; the same traversal succeeds against the deployed stack |

## 10. Error handling decisions

- **The model loader fails closed.** Digest mismatch refuses to load. This is the trust boundary and it does not degrade.
- **`/predict` returns 503 when the prediction cannot be persisted.** Complete prediction logging is an explicit rubric requirement (2.2) and the monitoring dashboard's drift and live-accuracy views are graded artifacts built on that table. Silently serving predictions that never reach the database would punch invisible holes in both. The write is synchronous with a bounded timeout and a single retry so a transient blip does not surface as an outage, and the failure is logged loudly. The accepted trade-off: the moderation endpoint is unavailable while the database is unavailable.
- **The re-scorer is idempotent, batched, and backs off.** Re-processing an item must not duplicate rows or double-advance status.
- **Deploy-time artifact fetch has a fallback.** A digest-pinned local or S3 mirror backs the W&B fetch, so a registry outage at bring-up does not turn the fail-closed loader into a demo outage.

## 11. Submission deliverables

Verify each in a logged-out browser before submitting.

- [ ] **Public GitHub repository URL** — opens without a login; `SECURITY.md` present; gitleaks-clean history
- [ ] **Public W&B project dashboard URL** — opens without a login; runs show git SHA, hyperparameters, `data_version`, and metrics including accuracy; no raw `input_text` was ever logged
- [ ] **Project workflow screenshots** — AWS Console showing three EC2 and RDS, plus the working prototype live on EC2, captured during the demo window, with no raw user text visible
- [ ] **Live prototype URL** — stable Elastic IP endpoint answering `/predict` and serving the UI, reachable after a stop/start cycle

Capture the screenshots and the reachability check while the stack is up. Do not stop the stack until they are done.

## 12. Accepted residual risk

| Risk | Why accepting it is defensible |
|---|---|
| No automated budget stop action | Owner decision, recorded in the AWS foundation spec §5.3. Compensated by the SCP instance-type allowlist, which is a hard denial rather than an alert, plus `terraform destroy` and session-scoped running |
| No SCP-enforceable RDS instance-class cap | `rds:DatabaseClass` is unsupported on `CreateDBInstance`, verified against the AWS service reference. The budget alarm and the Terraform-pinned class are the only controls. Known gap, not an oversight |
| Residual adversarial evasion | Cross-script homoglyphs and heavy paraphrase defeat normalization. Named in the model card. The review queue does not mitigate it, because a successful evasion is never flagged |
| DistilBERT pretraining contamination | The pretraining corpus may already contain these public comments. Not fixable or gradeable; naming it is the rigor |
| Single reviewer behind a shared secret | Not a real authentication system. Acceptable for a class project and named as such in the model card |

## 13. Supersessions

Where this spec conflicts with an earlier document, this spec governs.

| Document | Superseded passage |
|---|---|
| `docs/2026-07-01-toxic-moderation-mlops-design.md` §3, §13, §15 | Two-EC2 topology → three instances (§4 here) |
| Same, §4 | Uncalibrated classifier → `CalibratedClassifierCV`, capped `max_features` (§6.2) |
| Same, §7 | `review_queue` schema gains `source` and `input_text_snapshot` (§6.4) |
| Same, §9 | Safe-loading section extended with provenance, network, authz, and rendering rules (§6.3) |
| `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`, Phase A | Splits into A1 and A2 (§3.1) |
| Same, phase ordering | Strict A→0→1→2→3→4→5 → three slices (§3.2) |
| Same, AWS sizing table | Two instances → three, with EC2 #3 dropping to `t4g.small` if DistilBERT is cut (§4) |
| `docs/superpowers/plans/2026-07-01-phase-0-data-firewall.md` | Replaced wholesale by the hardened version recovered from `097d558` |
