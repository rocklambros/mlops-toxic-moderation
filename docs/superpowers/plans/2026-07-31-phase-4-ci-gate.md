# Phase 4: Test Consolidation and the CI/CD Gate

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pull request to `main` cannot merge unless ruff, the full pytest suite (unit and integration, against a real Postgres, above a coverage floor), gitleaks, semgrep, a dependency vulnerability audit, and `terraform fmt` + `validate` all pass — and the repository is configured so that the solo developer, who is the repository admin, cannot bypass that. Every dependency in the project installs from a `pip-compile --generate-hashes` lock through `--require-hashes`, every third-party GitHub Action is pinned to a full commit SHA, every job declares its own `permissions` block, and every container base image is pinned by digest.

**Architecture:** Three layers, in dependency order, because each one is worthless without the one below it.

```
LAYER 1  supply chain      hashed locks -> --require-hashes -> SHA-pinned actions -> digest-pinned images
              |            (nothing enters the build unverified, including the tools that verify)
LAYER 2  the checks        ruff | pytest unit+integration+coverage | gitleaks | semgrep | pip-audit | tf fmt+validate
              |            six jobs, each with its own least-privilege permissions block, fanning into
              |            one aggregate job `ci-gate` that fails if ANY upstream job did not succeed
LAYER 3  the enforcement   branch protection on `main`: required status check = `ci-gate`,
                           enforce_admins = true ("Do not allow bypassing the above settings")
                           + evidence, because layer 3 leaves no artifact in the repository
```

Layer 3 is the reason this phase exists in the form it does. Rubric 4.2's final sentence — "PRs cannot merge if checks fail" — is the single most explicitly-worded clause in the assignment, and it is **repository configuration, not code**. A grader cloning the repository sees `ci.yml` and green check marks and has no way to tell whether the merge button was ever actually disabled. So this phase produces four evidence artifacts under `docs/evidence/`, one of which is a screenshot of a merge that GitHub refused, and a pytest test that fails if any of them is missing or if the captured configuration shows admins can bypass.

**Tech Stack:** Python 3.11. pytest 8.3.3, pytest-cov 6.0.0, ruff 0.7.4, PyYAML 6.0.2 (workflow parsing in tests), pip-tools 7.4.1 (lock generation), pip-audit 2.7.3, semgrep 1.96.0, gitleaks 8.21.2 (checksum-verified release binary, not an action), Terraform (version read from a committed `.terraform-version`), GitHub Actions on `ubuntu-24.04-arm` runners, `postgres:16-alpine` as a digest-pinned service container, `gh` CLI 2.96.0 for the branch-protection API.

## Global Constraints

Inherited from the master roadmap and `docs/superpowers/specs/2026-07-30-delivery-plan-design.md`, which governs on conflict. The ones that bind Phase 4:

- **Rubric 4.2 is the contract.** `.github/workflows/ci.yml`, triggering on pull requests to `main`, running a linter and the full test suite, and PRs cannot merge if checks fail. All four clauses, not three.
- **Dependencies install from a hashed lock (`--require-hashes`), from day 1.** Delivery spec §2 and §6.3. The build box holds an AWS SSO refresh token, the W&B key, the Kaggle token, and the RunPod key simultaneously; one malicious transitive dependency in a post-install hook takes all four. Phase 0 established this. **Phase 4 verifies and enforces it repo-wide, including for the tools that do the verifying.**
- **Three EC2 instances** (backend, frontend, monitoring), each separate. Phase 4 touches none of them; it only refuses to let broken code reach them.
- **No AWS credentials in pull-request CI.** `gha-ci` is never assumed by `ci.yml`. No `id-token: write`, no `aws-actions/configure-aws-credentials`, no `terraform plan`.
- **Zero secrets in GitHub.** The repository has no Actions secrets and Phase 4 adds none. `GITHUB_TOKEN` is the only credential any job sees, and every job scopes it explicitly.
- **`PYTHONHASHSEED=0` everywhere pytest runs**, not just in the Makefile. Premortem H1.
- **Feature-branch + PR, human author** (`rocklambros <rock@rockcyber.com>`). No AI attribution in commits, code, or docs.
- **Repo is public.** Every file this phase creates is world-readable on push. No token, no account ID, no email beyond the committed author identity, no raw user text in any screenshot.
- **Schedule:** day 12 of 19 (delivery spec §7). The gate is on the never-cut list (§8).

**Branch:** `feat/phase-4-ci-gate` off `main`.

## File Structure

- `requirements/base.in`, `dev.in`, `serve.in`, `ui.in`, `monitor.in`, `security.in` — human-edited inputs.
- `requirements/base.txt`, `dev.txt`, `serve.txt`, `ui.txt`, `monitor.txt`, `security.txt` — compiled hashed locks.
- `requirements/pip-tools.txt` — hand-built hashed bootstrap lock for the lock generator itself.
- `scripts/__init__.py`, `scripts/pin_actions.py` — tag→SHA action pinner.
- `scripts/vuln_ledger.py` — suppression-ledger parser.
- `scripts/install_gitleaks.sh`, `scripts/gitleaks.sha256` — checksum-verified gitleaks install.
- `scripts/run_pip_audit.sh` — dependency audit driven by the ledger.
- `scripts/apply_branch_protection.sh`, `scripts/verify_branch_protection.sh` — rubric 4.2 layer 3.
- `.github/workflows/ci.yml` — the gate. Required status check context: `ci-gate`.
- `.terraform-version` — single source of truth for the Terraform version, local and CI.
- `tests/conftest.py` — marker auto-application, `PYTHONHASHSEED` guard, CI fake-green guard.
- `tests/unit/test_dependency_locks.py`, `test_install_commands.py`, `test_test_harness.py`, `test_coverage_policy.py`, `test_pin_actions.py`, `test_workflow_hygiene.py`, `test_vuln_ledger.py`, `test_image_pinning.py`, `test_interface_contracts.py`, `test_branch_protection.py`, `test_ci_gate_evidence.py`.
- `docs/security/pip-audit-ignores.md` — the suppression ledger.
- `docs/evidence/branch-protection.json`, `blocked-merge-api.txt`, `blocked-merge-cli.txt`, `blocked-merge.png`, `ci-gate.md`.
- `Makefile` — amended: `lock-tools`, `lock`, `deps`, `test-cov`, `scan`, `branch-protection`, `verify-branch-protection`.
- `pyproject.toml` — amended: markers, `--strict-markers`, `pythonpath`, coverage config.

## Interfaces Produced

```python
# scripts/pin_actions.py
pin_text(text: str, resolve: Callable[[str, str, str], str]) -> str
resolve_with_gh(owner: str, repo: str, ref: str) -> str          # 40-hex commit sha

# scripts/vuln_ledger.py
@dataclass(frozen=True) class Suppression:
    vuln_id: str; package: str; reason: str; expires: datetime.date
parse_ledger(text: str) -> list[Suppression]
active_ids(ledger: list[Suppression], today: datetime.date) -> list[str]
expired(ledger: list[Suppression], today: datetime.date) -> list[Suppression]
```

```
# shell entry points (all idempotent, all safe to re-run)
scripts/install_gitleaks.sh                       # -> ./bin/gitleaks, checksum verified
scripts/run_pip_audit.sh                          # exit 1 on any unsuppressed vulnerability
REPO= BRANCH= CONTEXT= scripts/apply_branch_protection.sh    # -> docs/evidence/branch-protection.json
scripts/verify_branch_protection.sh               # exit 1 on drift from the committed evidence
```

```
# make targets
make lock-tools   # rebuild requirements/pip-tools.txt from downloaded wheels + pip hash
make lock         # recompile every requirements/*.in into a hashed *.txt
make deps         # pip install --require-hashes -r requirements/dev.txt
make lint         # ruff check .
make test         # unit suite, PYTHONHASHSEED=0
make test-integration
make test-cov     # unit + integration with the coverage floor
make scan         # gitleaks + semgrep + pip-audit locally, same commands CI runs
make branch-protection / make verify-branch-protection
```

```yaml
# .github/workflows/ci.yml — the contract downstream phases depend on
jobs: lint, test, secrets-scan, sast, deps-audit, terraform, ci-gate
required status check context: "ci-gate"
```

## Interfaces Consumed

| From | Symbol / artifact | Used for |
|---|---|---|
| Phase 0 | `Makefile` (`venv`, `lint`, `test`, `data`), `pyproject.toml`, `requirements/base.txt`, `requirements/dev.txt`, `tests/unit/*` | Amended, not replaced |
| Phase 2 | `requirements/serve.in`, `requirements/serve.txt`, `backend/Dockerfile`, `Makefile` (`serve-deps`, `test-integration`), `tests/integration/conftest.py` | `serve.in` is re-pointed at `base.in`; `serve-deps` is folded into `make lock` |
| Phase 3 | `requirements/ui.txt`, `requirements/monitor.txt`, `Makefile` (`db-up`, `db-down`, `test-integration`), `TEST_DATABASE_URL` convention, `infra/docker-compose.yml` | Converted to `.in` + hashed lock; `TEST_DATABASE_URL` becomes the CI contract |
| Phase A2 | `infra/terraform/*.tf` | `fmt -check` and `validate` only. Never `plan` |
| Phase 1 | `.github/workflows/runpod-reaper.yml`, `requirements/train.in` | Asserted pinned and least-privileged **if present**; if cut, the cut must be recorded |

## Interface corrections this phase makes (premortem H24)

The master plan's Interface Contracts block is declared authoritative and has drifted in five places. Phase 2 reconciled its own slice by checklist; Phase 4 makes the reconciliation **executable** and repo-wide, because a checklist is exactly the kind of memo the premortem says disappears under schedule pressure.

| Master plan says | Reality after Phases 0–3 | Why it matters |
|---|---|---|
| `data_version: str  # sha256 over sorted deduped ids + config` | sha256 over the realized split + a per-id label fingerprint + pinned split-library versions | Pre-hardening semantics. An implementer building against it reproduces the collision the hardening removed |
| `prepare_dataset(raw_csv, config: SplitConfig) -> DatasetBundle` | `config: SplitConfig = SplitConfig()` | Callers written to the contract pass a config they do not need; the default is the documented entry point |
| `make_splits` absent from the block entirely | `make_splits(df, seed, test_size=0.15, n_folds=5)` | A Phase 0→1 seam with no contract row |
| `decision: str  # "allow" \| "review" \| "block"` | `Literal["allow", "review", "block"]` on `PredictionResponse` | A `str` contract does not reject `"delete"`; the implementation does |
| `insert_prediction(session, response, input_text)` | `write_pending(session, pending: PendingWrite, stamp) -> int`; `submit_review` / `write_distilbert_probs` moved to Phase 3 | The Phase 2 spool needs one code path for a prediction that has no `PredictionResponse` |
| Phase 0 test strategy: fixture "about 60 rows" | 36 rows | Stated counts that were never run are the premortem's evidence that the code was never executed |

Also corrected: the master plan's Phase 4 section (`ci.yml` "lint + tests + scans + tf plan gate on PR") still says `terraform plan` on PRs, which H36 removes.

## Premortem coverage map

Every row has an owning task whose test **fails if the finding is unfixed**.

| Id | Finding | Owning task | Test that fails if unfixed |
|---|---|---|---|
| **H10** | "PRs cannot merge if checks fail" is a GitHub setting. Nothing configures it, nothing screenshots it, and the solo developer is the admin who bypasses it unless "Do not allow bypassing" is ticked | 12, 13 | `test_administrators_cannot_bypass`, `test_the_required_status_check_is_the_aggregate_gate`, `test_the_blocked_merge_api_refusal_is_recorded`, `test_the_blocked_merge_screenshot_is_a_real_png` |
| **H35** | Unpinned third-party Actions (any of which can mint the `gha-deploy` OIDC token), no per-job `permissions:` against a repo default of *write*, unpinned base images, ECR *basic* scanning which cannot see Python dependencies and gates nothing | 6, 7, 8, 9, 10 | `test_every_third_party_action_is_pinned_to_a_full_commit_sha`, `test_every_job_declares_its_own_permissions_block`, `test_every_dockerfile_base_image_is_pinned_by_digest`, `test_every_service_container_is_pinned_by_digest`, `test_ci_runs_a_dependency_audit_that_can_fail_the_build` |
| **H36** | `terraform plan` on pull requests is code execution on attacker-supplied `.tf`, and the rubric does not ask for it | 7, 8 | `test_ci_never_runs_terraform_plan`, `test_ci_never_configures_aws_credentials`, `test_no_workflow_uses_pull_request_target` |
| **C11** | The build box holds four live credentials and the first `pip install` on it is unhashed. `--require-hashes` from day 1 | 1, 2, 3 | `test_every_lockfile_pins_every_requirement_with_a_hash`, `test_no_install_command_escapes_require_hashes`, `test_the_lock_generator_is_itself_installed_from_a_hashed_lock` |
| **H1** (CI half) | CI runs bare `pytest`, not `make test`, so the Makefile's `PYTHONHASHSEED` pin does not apply | 4, 7 | `test_pytest_refuses_to_run_without_a_pinned_hash_seed`, `test_ci_sets_pythonhashseed` |
| **H24** | The "authoritative" Interface Contracts block drifted in five places; the hardening commit never updated it | 11 | `test_every_contract_symbol_exists_with_the_declared_parameters`, `test_the_contracts_block_carries_no_superseded_text` |
| **C9** (rubric 4.2 rows) | Four rubric clauses had no owning task; two of them are 4.2's | 7, 8, 12, 13, 14 | `test_ci_triggers_on_pull_requests_to_main`, `test_ci_runs_a_linter_and_the_full_test_suite`, plus the Task 14 clause-by-clause self-check |
| Rubric 4.1 | Unit tests for individual functions, integration tests for FastAPI endpoints, with markers and a coverage floor | 4, 5 | `test_directory_layout_drives_the_markers`, `test_the_coverage_floor_is_declared_where_it_is_enforced`, `test_integration_tests_cannot_silently_skip_in_ci` |
| Anti-theatre | A gate that cannot fail is not a gate | 7, 9, 13 | `test_no_step_is_marked_continue_on_error`, `test_every_suppression_has_a_reason_and_an_unexpired_date`, and Task 13, which proves the block empirically |

**Explicitly not owned by Phase 4**, listed so the gap is visible rather than assumed: **H4** (the `gha-deploy` OIDC `sub` OR-bug and role scoping — Phase A2 `oidc.tf`; Phase 4 only guarantees that no PR-triggered job can reach that role at all), **H5** (SSM fire-and-forget — Phase 5 `deploy.yml`), **C7** (`terraform apply` on every push, AMI pinning, `paths-ignore` — Phase A2/5), **H27** (`awslogs` driver, health alarm — Phase 5), **H32** (README — Phase 5 day 15), **C5** (`make seed-demo` — Phase 3), **H31** (fairness slice — Phase 1/5 model card). Phase 4's repo-wide workflow-hygiene suite *does* apply to `deploy.yml` and `runpod-reaper.yml` when they land, so those phases inherit the pinning and permissions constraints as failing tests rather than as advice.

## Design decisions this phase must make explicitly

**D1 — one required status check, not six (H10).** Branch protection pins check *contexts* by name. Requiring six job names means every future job added or renamed silently stops being required, and a renamed job leaves a required context that never reports, deadlocking every merge. So the workflow ends in a single `ci-gate` job that `needs` all six, runs `if: always()`, and fails if any upstream result is not `success`. `ci-gate` is the only protected context. `if: always()` is load-bearing: without it, a *skipped* upstream job would let `ci-gate` be skipped, and a skipped required check reports as pending forever.

**D2 — `enforce_admins: true` but `required_pull_request_reviews: null` (H10).** These interact in a way that can deadlock a solo project. "Do not allow bypassing the above settings" is `enforce_admins`, and it is exactly what rubric 4.2 needs, because without it the repository admin — who is the only developer — merges straight through a red gate. But if a review requirement is *also* set, `enforce_admins` means the solo developer cannot merge anything at all, because GitHub does not let you approve your own pull request. The correct configuration is therefore: required status checks **on**, admin bypass **off**, review requirement **absent**. That satisfies the rubric clause precisely and leaves the project shippable. It is written down here because discovering it at 2 a.m. on day 12 costs a day.

**D3 — classic branch protection, not a ruleset.** GitHub's newer rulesets can express the same policy (an empty bypass list is the equivalent of "do not allow bypassing"), but they are served by a different API, so `repos/{repo}/branches/main/protection` returns 404 and the evidence capture and its test both break. This phase uses classic branch protection so that one command produces the evidence and one test verifies it. The console fallback in Task 12 is also classic, for the same reason.

**D4 — no `terraform plan` on pull requests (H36).** `plan` executes provider binaries, `data "external"` programs, and module `source` fetches against `.tf` files that a pull request author controls. It is not currently reachable from a fork, but `pull_request_target` and `workflow_run` both reintroduce it, and once a repository has a plan job the pressure to make it work on forks is constant. The rubric asks for a linter and the test suite; it does not ask for a plan. `fmt -check` and `validate` catch the errors a plan would catch at this project's scale, and `validate` runs after `terraform init -backend=false`, which needs no state, no backend, and no credentials.

**D5 — ECR scan-on-push is not a dependency scan.** The AWS foundation spec §7.2 enables ECR scan on push. That is Amazon ECR *basic* scanning, which is a CVE match against the operating-system package database (Clair-derived) and **does not read Python distributions at all**. Every dependency this project actually runs — scikit-learn, FastAPI, streamlit, SQLAlchemy, and their transitive closure — is invisible to it. It also gates nothing: a finding does not fail a build, it writes a report. So it stays for the OS layer and is not counted as a control, and the real dependency scan is `pip-audit` running in CI against every shipped lock, failing the job on any unsuppressed vulnerability. Enhanced (Inspector) scanning would read the Python layer but is an account-level subscription with a per-image charge, which is out of budget for a class project; this is a stated trade, not an oversight.

**D6 — gitleaks by checksum-verified binary, semgrep and pip-audit from a hashed lock.** Every scanner is a program that reads the whole repository. Adding one as an unpinned action adds it to the OIDC blast radius H35 describes. semgrep and pip-audit are Python distributions, so the hashed lock already covers them and they need no action at all. gitleaks is a Go binary; rather than trust a third-party Docker action, this phase downloads the pinned release tarball and verifies it against a checksum file committed to the repository, so the scanner is under the same integrity rule as everything it scans. Residual, named: semgrep's `p/python` and `p/secrets` rule packs are fetched from the semgrep registry at run time and are not content-pinned. Vendoring them is possible and is deliberately not done — the rules are analysis inputs, not executed code, and pinning them would freeze the SAST coverage at day 12.

**D7 — the coverage floor is 80, on `model`, `backend`, and `monitoring`.** Streamlit entry points (`frontend/ui.py`, `monitoring/dashboard.py`) are omitted because they are render code whose only meaningful test is the Phase 3 integration traversal, and counting them would push the project toward writing coverage theatre against a UI. `rescorer` is not in the source list because it sits behind the day-8 cut-line and a `source` entry for a package that was cut fails the run for the wrong reason. The floor is asserted in the Makefile, in `ci.yml`, and by a test, so it cannot be quietly lowered when a red build is inconvenient — which is the only moment anyone ever lowers a coverage floor.

---

### Task 1: A hashed bootstrap for the lock generator itself (premortem C11)

The lock generator is the first thing installed and the last thing anyone thinks to verify. `pip install pip-tools==7.4.1` is pinned but **unhashed**, and it runs on the box holding the AWS SSO refresh token, the W&B key, the Kaggle token, and the RunPod key. This task closes the loop so that no `pip install` in the repository — including the one that builds the locks — has an exception.

**Files:**
- Create: `requirements/pip-tools.txt`
- Modify: `Makefile`, `.gitignore`
- Test: `tests/unit/test_dependency_locks.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_dependency_locks.py`:
```python
"""Every dependency this project installs is pinned AND hashed (premortem C11).

The build box holds an AWS SSO refresh token, the W&B key, the Kaggle token, and the RunPod
key at the same time. One malicious transitive dependency with a post-install hook takes all
four, and the blast radius is the AWS organisation rather than the sandbox. `--require-hashes`
is what makes "pinned" mean "this exact file" instead of "this version string, whatever the
index serves today".
"""

import re
from pathlib import Path

REQUIREMENTS = Path("requirements")
HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}")
PINNED_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]+\])?==(?P<version>\S+)")


def lockfiles() -> list[Path]:
    return sorted(REQUIREMENTS.glob("*.txt"))


def requirement_blocks(text: str) -> list[tuple[str, str]]:
    """Split a lock into (name, block) pairs, joining pip's backslash continuations."""
    blocks: list[tuple[str, str]] = []
    name: str | None = None
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        match = PINNED_RE.match(line)
        if match and not raw.startswith((" ", "\t")):
            if name is not None:
                blocks.append((name, "\n".join(current)))
            name = match.group("name").lower().replace("_", "-")
            current = [line]
        elif name is not None:
            current.append(line)
    if name is not None:
        blocks.append((name, "\n".join(current)))
    return blocks


def test_the_lock_generator_is_itself_installed_from_a_hashed_lock():
    bootstrap = REQUIREMENTS / "pip-tools.txt"
    assert bootstrap.exists(), (
        "requirements/pip-tools.txt is missing: pip-tools is installed unhashed, on the box "
        "that holds four live credentials (premortem C11)"
    )
    blocks = requirement_blocks(bootstrap.read_text(encoding="utf-8"))
    names = {name for name, _ in blocks}
    assert "pip-tools" in names, f"pip-tools itself is not pinned in the bootstrap lock: {names}"
    for name, block in blocks:
        assert HASH_RE.search(block), f"{name} in pip-tools.txt carries no --hash"


def test_the_bootstrap_lock_carries_the_full_transitive_closure():
    """`pip install --require-hashes` refuses to resolve anything not listed, so a bootstrap
    lock that names only pip-tools installs nothing at all."""
    blocks = requirement_blocks((REQUIREMENTS / "pip-tools.txt").read_text(encoding="utf-8"))
    names = {name for name, _ in blocks}
    for dependency in ("build", "click", "pyproject-hooks", "wheel"):
        assert dependency in names, (
            f"{dependency} is a pip-tools dependency and is absent from the bootstrap lock; "
            "regenerate it with `make lock-tools`"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dependency_locks.py -v`
Expected: FAIL — `test_the_lock_generator_is_itself_installed_from_a_hashed_lock` with `AssertionError: requirements/pip-tools.txt is missing: pip-tools is installed unhashed, on the box that holds four live credentials (premortem C11)`, and `test_the_bootstrap_lock_carries_the_full_transitive_closure` with `FileNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

Append to the `Makefile` (tabs, not spaces, for recipe lines):
```makefile
.PHONY: lock-tools
# Bootstrap the lock generator without executing any package code. `pip download` with
# --only-binary=:all: fetches wheels, which are zip archives that pip does not execute at
# download time; an sdist WOULD run setup.py for metadata, which is the hole this closes.
lock-tools:
	rm -rf build/pip-tools-wheels
	mkdir -p build/pip-tools-wheels requirements
	$(BIN)/python -m pip download --only-binary=:all: --dest build/pip-tools-wheels pip-tools==7.4.1
	: > requirements/pip-tools.txt
	@for whl in build/pip-tools-wheels/*.whl; do \
	  name=$$(basename $$whl | cut -d- -f1 | tr '_' '-'); \
	  version=$$(basename $$whl | cut -d- -f2); \
	  hash=$$($(BIN)/python -m pip hash $$whl | tail -1); \
	  printf '%s==%s \\\n    %s\n' "$$name" "$$version" "$$hash" >> requirements/pip-tools.txt; \
	done
	@echo "wrote requirements/pip-tools.txt"
	$(BIN)/python -m pip install --require-hashes -r requirements/pip-tools.txt
```

Append to `.gitignore`:
```
build/
bin/gitleaks
gitleaks.sarif
.coverage
.coverage.*
htmlcov/
```

Then generate it:
```bash
make lock-tools
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dependency_locks.py -v`
Expected: 2 PASS. `requirements/pip-tools.txt` contains one `name==version \` line per wheel followed by an indented `--hash=sha256:...`, and `pip-compile --version` reports `7.4.1`.

- [ ] **Step 5: Commit**

```bash
git add requirements/pip-tools.txt Makefile .gitignore tests/unit/test_dependency_locks.py
git commit -m "Bootstrap pip-tools from a hashed lock built from downloaded wheels"
```

---

### Task 2: Every requirements surface becomes a compiled hashed lock (premortem C11, H35)

Phase 0 shipped `base.txt` and `dev.txt` as plain `==` pins. Phase 2 shipped `serve.in` → hashed `serve.txt`. Phase 3 shipped `ui.txt` and `monitor.txt` as plain pins. The result is that three of five surfaces install unhashed, and the two Streamlit images — one of which is the internet-facing frontend — are among them. This task makes the shape uniform: one `.in` per surface, one compiled hashed `.txt` per `.in`, one command to regenerate all of them.

**Files:**
- Create: `requirements/base.in`, `requirements/ui.in`, `requirements/monitor.in`, `requirements/security.in`, `requirements/dev.in`
- Modify: `requirements/serve.in` (re-point at `base.in`), `Makefile`
- Delete: nothing — `pip-compile` overwrites the `.txt` files in place
- Test: `tests/unit/test_dependency_locks.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_dependency_locks.py`:
```python
SURFACES = ("base", "dev", "serve", "ui", "monitor", "security")


def test_every_surface_has_a_human_edited_input_file():
    missing = [s for s in SURFACES if not (REQUIREMENTS / f"{s}.in").exists()]
    assert not missing, f"no .in input for: {missing}. Hand-edited locks drift silently"


def test_every_lockfile_pins_every_requirement_with_a_hash():
    offenders = []
    for lock in lockfiles():
        for name, block in requirement_blocks(lock.read_text(encoding="utf-8")):
            if not HASH_RE.search(block):
                offenders.append(f"{lock.name}:{name}")
    assert not offenders, (
        "these requirements install without hash verification (premortem C11): "
        + ", ".join(offenders)
    )


def test_every_compiled_lock_records_the_input_it_came_from():
    """pip-compile writes the exact command into the header. If the header does not name a
    .in file, the lock was hand-edited and the next `make lock` silently reverts it."""
    for lock in lockfiles():
        if lock.name == "pip-tools.txt":
            continue  # hand-built by `make lock-tools`, which the previous test covers
        header = lock.read_text(encoding="utf-8")[:1200]
        assert "pip-compile" in header, f"{lock.name} was not produced by pip-compile"
        assert f"requirements/{lock.stem}.in" in header, (
            f"{lock.name} does not name requirements/{lock.stem}.in as its input"
        )


def test_no_lockfile_carries_an_unpinned_or_ranged_requirement():
    ranged = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]*\s*(>=|<=|~=|>|<|!=)")
    offenders = []
    for lock in lockfiles():
        for lineno, raw in enumerate(lock.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if line and ranged.match(line):
                offenders.append(f"{lock.name}:{lineno} {line}")
    assert not offenders, f"version ranges in a lock defeat the lock: {offenders}"


def test_the_dev_lock_is_the_superset_the_test_job_installs():
    """CI installs one lock. If it does not carry the serving, UI, and monitoring
    dependencies, the integration suite imports them from whatever is already on the runner."""
    dev_in = (REQUIREMENTS / "dev.in").read_text(encoding="utf-8")
    for surface in ("base", "serve", "ui", "monitor"):
        assert f"-r {surface}.in" in dev_in, f"dev.in does not include {surface}.in"
    dev_lock = (REQUIREMENTS / "dev.txt").read_text(encoding="utf-8")
    names = {name for name, _ in requirement_blocks(dev_lock)}
    for package in ("pytest", "pytest-cov", "ruff", "pyyaml", "fastapi", "streamlit"):
        assert package in names, f"{package} is absent from the compiled dev lock"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dependency_locks.py -v`
Expected: FAIL — `test_every_surface_has_a_human_edited_input_file` with `AssertionError: no .in input for: ['base', 'dev', 'ui', 'monitor', 'security']`, and `test_every_lockfile_pins_every_requirement_with_a_hash` listing every requirement in `base.txt`, `dev.txt`, `ui.txt`, and `monitor.txt`.

- [ ] **Step 3: Write minimal implementation**

`requirements/base.in`:
```
numpy==2.1.3
pandas==2.2.3
scipy==1.14.1
scikit-learn==1.5.2
skops==0.11.0
iterative-stratification==0.1.9
datasketch==1.6.5
pydantic==2.9.2
```

`requirements/serve.in` — replace the first line only. Phase 2 wrote `-r base.txt`, which now points at a compiled artifact rather than an input:
```
-r base.in
fastapi==0.115.5
uvicorn==0.32.1
SQLAlchemy==2.0.36
psycopg[binary]==3.2.3
```

`requirements/ui.in`:
```
-r base.in
streamlit==1.39.0
httpx==0.27.2
```

`requirements/monitor.in`:
```
-r base.in
streamlit==1.39.0
SQLAlchemy==2.0.36
psycopg[binary]==3.2.3
```

`requirements/security.in` — deliberately isolated from `dev.in`. A semgrep resolution failure on aarch64 must not be able to block the test suite from being lockable:
```
pip-audit==2.7.3
semgrep==1.96.0
```

`requirements/dev.in`:
```
-r base.in
-r serve.in
-r ui.in
-r monitor.in
pytest==8.3.3
pytest-cov==6.0.0
ruff==0.7.4
PyYAML==6.0.2
testcontainers==4.8.2
```

Append to the `Makefile`:
```makefile
.PHONY: lock deps
# PIP_ONLY_BINARY=:all: keeps lock generation from building an sdist, which is the one moment
# in this workflow where third-party code could execute on the credential-bearing build box.
# If a package genuinely ships no wheel, the compile fails loudly here rather than silently
# running setup.py; the fix is to add that ONE package to PIP_NO_BINARY with a written reason.
LOCK_SURFACES := base serve ui monitor security dev train
lock:
	$(BIN)/python -m pip install --require-hashes -r requirements/pip-tools.txt
	@for surface in $(LOCK_SURFACES); do \
	  test -f requirements/$$surface.in || continue; \
	  echo "compiling requirements/$$surface.in"; \
	  PIP_ONLY_BINARY=:all: $(BIN)/pip-compile --quiet --generate-hashes \
	    --output-file requirements/$$surface.txt requirements/$$surface.in || exit 1; \
	done
deps:
	$(BIN)/python -m pip install --require-hashes -r requirements/dev.txt
```

Remove the now-superseded `serve-deps` target that Phase 2 added (its `pip install pip-tools` line is the unhashed install Task 3 forbids), and generate everything:
```bash
make lock
make deps
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_dependency_locks.py -v && .venv/bin/ruff check .`
Expected: 7 PASS; every `requirements/*.txt` opens with a `pip-compile --generate-hashes --output-file=requirements/<surface>.txt requirements/<surface>.in` header and carries `--hash=sha256:` on every requirement; ruff clean.

- [ ] **Step 5: Commit**

```bash
git add requirements Makefile tests/unit/test_dependency_locks.py
git commit -m "Compile every dependency surface into a hashed lock"
```

---

### Task 3: No install path escapes `--require-hashes` (premortem C11)

Locks are only worth what the install commands respect. This task scans every place in the repository that installs a Python package — the Makefile, every Dockerfile, every workflow, every shell script — and fails if any of them installs without hash verification. It has **no exception list**, which is the property that makes it survive: an exception list is a place to hide.

**Files:**
- Test: `tests/unit/test_install_commands.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_install_commands.py`:
```python
"""Every `pip install` in this repository verifies hashes (premortem C11, delivery spec §6.3).

There is no allowlist. `pip install --upgrade pip` is forbidden too: bootstrapping the tool
that checks integrity by fetching it without checking integrity is the exact circularity this
control exists to remove. The interpreter's bundled pip is sufficient.
"""

import re
from pathlib import Path

SEARCH_ROOTS = ("Makefile", ".github", "backend", "frontend", "monitoring", "rescorer",
                "infra", "scripts", "model")
SKIP_PARTS = {".venv", "build", "node_modules", ".git", "__pycache__"}
INSTALL_RE = re.compile(r"(?:python\s+-m\s+|\$\([A-Z_]+\)/|\./|[\w/.$(){}-]*/)?pip3?\s+install\b[^\n;&|]*")


def candidate_files() -> list[Path]:
    found: list[Path] = []
    for root in SEARCH_ROOTS:
        path = Path(root)
        if not path.exists():
            continue
        if path.is_file():
            found.append(path)
            continue
        for child in path.rglob("*"):
            if not child.is_file() or SKIP_PARTS & set(child.parts):
                continue
            if child.suffix in {".yml", ".yaml", ".sh", ".py"} or child.name.startswith("Dockerfile"):
                found.append(child)
    return sorted(set(found))


def install_commands(text: str) -> list[str]:
    """Join backslash continuations so a flag on the next line still counts as the same command."""
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    return [m.group(0) for m in INSTALL_RE.finditer(joined)]


def test_the_scanner_actually_looks_at_something():
    files = candidate_files()
    assert files, "the install scanner found no files to scan; SEARCH_ROOTS is wrong"
    assert any(p.name == "Makefile" for p in files)


def test_no_install_command_escapes_require_hashes():
    offenders = []
    for path in candidate_files():
        if path.name == "test_install_commands.py":
            continue
        for command in install_commands(path.read_text(encoding="utf-8", errors="replace")):
            if "--require-hashes" not in command:
                offenders.append(f"{path}: {command.strip()}")
    assert not offenders, (
        "these installs run without hash verification on a box holding live credentials "
        "(premortem C11):\n  " + "\n  ".join(offenders)
    )


def test_pip_itself_is_never_upgraded_from_the_network():
    offenders = []
    for path in candidate_files():
        if path.name == "test_install_commands.py":
            continue
        text = re.sub(r"\\\s*\n\s*", " ", path.read_text(encoding="utf-8", errors="replace"))
        for match in re.finditer(r"pip3?\s+install\b[^\n;&|]*", text):
            command = match.group(0)
            if re.search(r"(--upgrade|-U)\b[^\n]*\bpip\b", command):
                offenders.append(f"{path}: {command.strip()}")
    assert not offenders, f"bootstrapping pip over the network defeats the lock: {offenders}"


def test_no_dockerfile_installs_from_an_unpinned_apt_or_curl_pipe():
    offenders = []
    for path in candidate_files():
        if not path.name.startswith("Dockerfile"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"curl[^|\n]*\|\s*(ba)?sh", line):
                offenders.append(f"{path}:{lineno} curl-pipe-to-shell")
    assert not offenders, f"unverified remote code at image build time: {offenders}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_install_commands.py -v`
Expected: FAIL — `test_no_install_command_escapes_require_hashes` naming `Makefile: $(BIN)/python -m pip install -r requirements/dev.txt` (the Phase 0 `venv` target) and any remaining `pip install pip-tools==7.4.1`.

- [ ] **Step 3: Write minimal implementation**

Amend the Phase 0 `venv` target in the `Makefile` so the very first install on the credential-bearing box is hash-verified. This is the line the premortem names in C11:
```makefile
venv:
	$(PY) -m venv $(VENV)
	$(BIN)/python -m pip install --require-hashes -r requirements/dev.txt
```

If any other offender remains — a Dockerfile, a workflow, a script — add `--require-hashes` to it and point it at the compiled lock for that surface. Phase 2's `backend/Dockerfile` already installs with `--require-hashes --no-deps -r requirements/serve.txt` and needs no change.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_install_commands.py -v`
Expected: 4 PASS.

Then prove the control from a clean environment, which is the only way to know the lock is complete:
```bash
rm -rf .venv && make venv && make deps && PYTHONHASHSEED=0 .venv/bin/pytest -m "not integration" -q
```
Expected: the venv builds, every install prints hash-checked resolution, and the unit suite is green.

- [ ] **Step 5: Commit**

```bash
git add Makefile tests/unit/test_install_commands.py
git commit -m "Require hash verification on every dependency install path"
```

---

### Task 4: Markers, the `PYTHONHASHSEED` guard, and the fake-green guard (premortem H1, rubric 4.1)

Rubric 4.1 asks for unit tests and integration tests. Having both is not enough — CI has to be able to run them separately, they have to be marked without anyone remembering to mark them, and the integration half must not be able to report green when it never connected to anything. H1 is the concrete version of the same failure: the Makefile pinned `PYTHONHASHSEED=0`, CI ran bare `pytest`, and the pin therefore did not apply where it mattered most.

**Files:**
- Create: `tests/conftest.py`
- Modify: `pyproject.toml`
- Test: `tests/unit/test_test_harness.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_test_harness.py`:
```python
"""The test harness enforces its own preconditions (premortem H1, rubric 4.1).

Three properties, each of which was a memo before it was a test:
  1. PYTHONHASHSEED=0 is set for EVERY pytest invocation, not only `make test`.
  2. Markers follow the directory layout, so `-m "not integration"` is trustworthy.
  3. Integration tests cannot silently skip inside CI and report a green job.
"""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_pytest(args: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def test_pytest_refuses_to_run_without_a_pinned_hash_seed():
    """H1: `MinHashLSH.query` returns `list(set(...))`, whose order varies with the hash seed,
    so which representative absorbs a duplicate's labels — and therefore `data_version` —
    is environment-dependent. A bare `pytest` in CI silently reintroduces that."""
    result = run_pytest(
        ["tests/unit/test_labels.py", "-q"], {"PYTHONHASHSEED": "random"}
    )
    assert result.returncode != 0, "pytest ran with an unpinned hash seed"
    assert "PYTHONHASHSEED=0 is required" in (result.stdout + result.stderr)


def test_integration_tests_cannot_silently_skip_in_ci():
    result = run_pytest(
        ["tests/unit/test_labels.py", "-q"],
        {"PYTHONHASHSEED": "0", "CI": "true", "TEST_DATABASE_URL": ""},
    )
    assert result.returncode != 0, "CI would have reported green without a database"
    assert "TEST_DATABASE_URL is unset" in (result.stdout + result.stderr)


def test_directory_layout_drives_the_markers():
    unit = run_pytest(["--collect-only", "-q", "-m", "unit"], {"PYTHONHASHSEED": "0"})
    assert unit.returncode == 0, unit.stdout + unit.stderr
    assert "tests/unit/test_labels.py" in unit.stdout, "unit tests were not auto-marked"

    integration = run_pytest(
        ["--collect-only", "-q", "-m", "integration"], {"PYTHONHASHSEED": "0"}
    )
    assert "tests/unit/" not in integration.stdout, (
        "a unit test was collected under -m integration; the marker hook mis-classified it"
    )


def test_markers_are_declared_and_strict():
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    assert "--strict-markers" in options["addopts"], (
        "without --strict-markers a typo'd marker silently selects nothing"
    )
    declared = " ".join(options["markers"])
    assert "unit:" in declared and "integration:" in declared
    assert options.get("pythonpath") == ["."], "scripts/ must be importable by its tests"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_test_harness.py -v`
Expected: FAIL — `test_pytest_refuses_to_run_without_a_pinned_hash_seed` with `AssertionError: pytest ran with an unpinned hash seed` (the subprocess exits 0 because no guard exists), and `test_markers_are_declared_and_strict` with `AssertionError: without --strict-markers a typo'd marker silently selects nothing`.

- [ ] **Step 3: Write minimal implementation**

`tests/conftest.py`:
```python
"""Cross-cutting pytest configuration for the whole repository.

These are enforced here rather than documented because the premortem found CI running bare
`pytest` while only the Makefile pinned PYTHONHASHSEED (H1), and because a marker applied by
hand is a marker that gets forgotten on the file that matters.
"""

import os
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise pytest.UsageError(
            "PYTHONHASHSEED=0 is required. Set iteration order decides which row survives "
            "dedup and therefore what data_version hashes to (premortem H1). "
            "Run `make test`, or export PYTHONHASHSEED=0 before invoking pytest."
        )
    if os.environ.get("CI") == "true" and not os.environ.get("TEST_DATABASE_URL"):
        raise pytest.UsageError(
            "TEST_DATABASE_URL is unset inside CI. Integration tests would skip and the job "
            "would report a green that proves nothing."
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Markers follow the directory layout. tests/unit -> unit, tests/integration ->
    integration. Nothing is marked by hand, so nothing is forgotten."""
    for item in items:
        parts = Path(str(item.path)).parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        elif "perf" in parts:
            item.add_marker(pytest.mark.perf)
        elif "awsapply" in parts:
            item.add_marker(pytest.mark.awsapply)
        elif "unit" in parts:
            item.add_marker(pytest.mark.unit)
```

Replace the `[tool.pytest.ini_options]` block in `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
addopts = "-q --strict-markers --strict-config"
markers = [
    "unit: fast, no external services, applied automatically to tests/unit",
    "integration: needs external services, applied automatically to tests/integration (deselect with -m 'not integration')",
    "perf: wall-clock and latency budget checks under tests/perf (deselect with -m 'not perf')",
    "awsapply: needs a real AWS session; never runs in pull-request CI (premortem H36)",
]
```

> **Correction, 2026-07-31.** As originally written this block declared only `unit` and `integration` while turning on `--strict-markers`. Phase 1 Task 1 declares `perf: wall-clock budget checks`, Phase 2 Task 20 re-declares `perf: measures latency against a real database`, and A2 Task 21 declares `awsapply`. Under `--strict-markers` a marker that is used and not declared raises **at collection**, so `@pytest.mark.perf` in `tests/perf/test_fit_budget.py` and `tests/perf/test_latency_budget.py` and `pytestmark = pytest.mark.awsapply` in `tests/infra/test_plan_assertions.py` would error the entire suite, taking `make test` (`-m "not integration and not perf"`) and `make loadtest` (`-m perf`) with them. This block is the single declaration of record; Phase 1 Task 1 and Phase 2 Task 20 must not re-declare it, and `test_every_marker_used_in_the_tree_is_declared` below is what catches a fifth marker appearing later.

Append to `tests/unit/test_test_harness.py`:
```python
def test_every_marker_used_in_the_tree_is_declared():
    """--strict-markers turns an undeclared marker into a COLLECTION error, so one
    @pytest.mark.perf in a file nobody re-reads takes the whole suite down."""
    import tomllib
    from pathlib import Path

    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    declared = set(re.findall(r"^(\w+):", "\n".join(options["markers"]), re.M))
    used = set(
        re.findall(
            r"@?pytest\.mark\.(\w+)",
            "\n".join(p.read_text(encoding="utf-8") for p in Path("tests").rglob("*.py")),
        )
    )
    builtin = {"parametrize", "skip", "skipif", "xfail", "usefixtures", "filterwarnings"}
    assert used <= declared | builtin, sorted(used - declared - builtin)


def test_the_ci_database_guard_does_not_fire_on_the_unit_job():
    """The guard raises UsageError when CI=true and TEST_DATABASE_URL is unset. Phase 0's
    `test_guard_allows_run_with_pythonhashseed_zero` spawns a subprocess that inherits
    CI=true from GitHub Actions and asserts returncode == 0, so the unit job must export
    TEST_DATABASE_URL too. ci.yml (Task 8) is where that is done; this is where it is
    asserted, because the failure only appears inside Actions."""
    from pathlib import Path

    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    unit_job = workflow.split("unit:")[1].split("\n  ", 1)[0]
    assert "TEST_DATABASE_URL" in unit_job, (
        "the unit job runs with CI=true; without TEST_DATABASE_URL the conftest guard "
        "raises UsageError and Phase 0's subprocess guard test goes red inside Actions"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_test_harness.py -v`
Expected: 5 PASS. `test_the_ci_database_guard_does_not_fire_on_the_unit_job` stays red until Task 8 writes `ci.yml`; re-run it at the end of Task 8 and treat it as part of that task's gate.

Then confirm the guard did not break the existing suites:
```bash
make test && make lint
```
Expected: the full unit suite green, ruff clean. A bare `.venv/bin/pytest` now exits non-zero with the `PYTHONHASHSEED=0 is required` usage error, which is the intended behaviour.

- [ ] **Step 5: Commit**

```bash
git add tests/conftest.py pyproject.toml tests/unit/test_test_harness.py
git commit -m "Enforce seed pinning, directory-driven markers, and no silent integration skips"
```

---

### Task 5: A coverage floor that cannot be quietly lowered (rubric 4.1)

A coverage number that lives only in a CI command is lowered the first time it is inconvenient, at 1 a.m., in the same commit as the fix that broke it. This task puts the floor in the Makefile, in the coverage configuration, and under a test that asserts the exact value, so lowering it is a visible, reviewable act.

**Files:**
- Modify: `pyproject.toml`, `Makefile`
- Test: `tests/unit/test_coverage_policy.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_coverage_policy.py`:
```python
"""The coverage floor is 80% on the code that decides outcomes (rubric 4.1).

Streamlit entry points are omitted deliberately: their only meaningful test is the Phase 3
end-to-end traversal, and counting them pushes the project toward coverage theatre against a
UI. `rescorer` is absent from the source list because it sits behind the day-8 cut-line, and a
source entry for a cut package fails the run for the wrong reason.
"""

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOOR = "--cov-fail-under=80"


def pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_coverage_floor_is_declared_where_it_is_enforced():
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert FLOOR in makefile, (
        f"{FLOOR} is not in the Makefile; the floor exists only in whatever CI happens to run"
    )


def test_coverage_measures_the_code_that_decides_outcomes():
    run = pyproject()["tool"]["coverage"]["run"]
    assert run["branch"] is True, "line coverage alone hides an untaken policy branch"
    assert set(run["source"]) == {"model", "backend", "monitoring"}, run["source"]


def test_the_streamlit_entry_points_are_omitted_on_purpose_not_by_accident():
    omit = pyproject()["tool"]["coverage"]["run"]["omit"]
    assert "frontend/ui.py" in omit
    assert "monitoring/dashboard.py" in omit


def test_no_module_is_excluded_by_a_blanket_pragma():
    """`# pragma: no cover` on a whole module is how a coverage floor is defeated without
    changing the number."""
    offenders = []
    for package in ("model", "backend", "monitoring"):
        root = REPO_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            head = path.read_text(encoding="utf-8").splitlines()[:5]
            if any("pragma: no cover" in line for line in head):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"module-level coverage exclusions: {offenders}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_coverage_policy.py -v`
Expected: FAIL — `test_the_coverage_floor_is_declared_where_it_is_enforced` with `AssertionError: --cov-fail-under=80 is not in the Makefile...`, and `test_coverage_measures_the_code_that_decides_outcomes` with `KeyError: 'coverage'`.

- [ ] **Step 3: Write minimal implementation**

Append to `pyproject.toml`:
```toml
[tool.coverage.run]
branch = true
source = ["model", "backend", "monitoring"]
omit = [
    "frontend/ui.py",
    "monitoring/dashboard.py",
    "*/__main__.py",
]

[tool.coverage.report]
show_missing = true
skip_covered = true
exclude_also = [
    "if __name__ == .__main__.:",
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
```

Append to the `Makefile`. Coverage is appended across the two runs so the number reflects the whole suite rather than the unit half:
```makefile
.PHONY: test-cov
test-cov:
	PYTHONHASHSEED=0 $(BIN)/pytest -m "not integration" --cov --cov-report=
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) PYTHONHASHSEED=0 $(BIN)/pytest -m integration \
	  --cov --cov-append --cov-report=term-missing:skip-covered --cov-fail-under=80
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_coverage_policy.py -v`
Expected: 4 PASS.

Then measure for real:
```bash
make db-up && make test-cov
```
Expected: `Required test coverage of 80% reached` (or higher). **If the run reports below 80, do not lower the floor.** The `term-missing:skip-covered` report names the exact uncovered lines; write tests for the modules at the top of that list before continuing. The uncovered surface at this point in the project is by construction the code no phase had a reason to exercise, which is precisely the code most likely to be wrong.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml Makefile tests/unit/test_coverage_policy.py
git commit -m "Set a branch-coverage floor of 80 percent on the decision-making code"
```

---

### Task 6: The action pinner (premortem H35)

Any action in any job can be swapped under its tag by whoever controls that tag, and `deploy.yml` runs jobs that mint the `gha-deploy` OIDC token. A mutable tag in *any* workflow is therefore a supply-chain hole with production blast radius. This task builds the tool that pins them, before there is a workflow to pin — so the workflow is never committed unpinned even once, and so no fabricated SHA ever enters the repository.

**Files:**
- Create: `scripts/__init__.py`, `scripts/pin_actions.py`
- Test: `tests/unit/test_pin_actions.py`

**Interfaces produced:** `pin_text(text, resolve) -> str`, `resolve_with_gh(owner, repo, ref) -> str`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_pin_actions.py`:
```python
"""The tag->SHA pinner (premortem H35). Pure function, fake resolver, no network."""

import pytest

from scripts.pin_actions import pin_text

SHA = "0" * 39 + "1"


def fake_resolver(owner: str, repo: str, ref: str) -> str:
    assert ref and not ref.startswith("#")
    return SHA


def test_a_tagged_action_is_rewritten_to_a_sha_with_the_tag_kept_as_a_comment():
    text = "    steps:\n      - uses: actions/checkout@v4.2.2\n"
    assert pin_text(text, fake_resolver) == (
        f"    steps:\n      - uses: actions/checkout@{SHA}  # v4.2.2\n"
    )


def test_an_action_with_a_subpath_keeps_the_subpath():
    text = "      - uses: github/codeql-action/init@v3\n"
    assert f"github/codeql-action/init@{SHA}  # v3" in pin_text(text, fake_resolver)


def test_pinning_is_idempotent():
    once = pin_text("      - uses: actions/checkout@v4.2.2\n", fake_resolver)
    assert pin_text(once, fake_resolver) == once


def test_a_local_action_is_left_alone():
    text = "      - uses: ./.github/actions/setup\n"
    assert pin_text(text, fake_resolver) == text


def test_a_docker_action_reference_is_left_alone():
    text = "      - uses: docker://alpine:3.20\n"
    assert pin_text(text, fake_resolver) == text


def test_a_resolver_that_returns_a_tag_is_rejected():
    def liar(owner: str, repo: str, ref: str) -> str:
        return "v4.2.2"

    with pytest.raises(ValueError, match="non-sha"):
        pin_text("      - uses: actions/checkout@v4.2.2\n", liar)


def test_lines_that_are_not_uses_are_untouched():
    text = "    # uses: actions/checkout@v4\n    run: echo uses: actions/checkout@v4\n"
    assert pin_text(text, fake_resolver) == text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_pin_actions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts'` (collection error on all seven tests).

- [ ] **Step 3: Write minimal implementation**

Create an empty `scripts/__init__.py`.

`scripts/pin_actions.py`:
```python
"""Rewrite GitHub Actions `uses:` references from mutable tags to full commit SHAs.

Premortem H35: any action in any job can mint the OIDC token the deploy role trusts, so a tag
that its owner can move is a supply-chain hole with production blast radius. This resolves each
tag once, writes the 40-character commit SHA into the workflow, and leaves the tag behind as a
trailing comment so the pin stays auditable and upgradable by a human.

Usage:  python -m scripts.pin_actions [workflow ...]
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

USES_RE = re.compile(
    r"^(?P<indent>\s*(?:-\s+)?)uses:\s*"
    r"(?P<owner>[A-Za-z0-9][\w.-]*)/(?P<repo>[\w.-]+)"
    r"(?P<subpath>(?:/[\w.-]+)*)"
    r"@(?P<ref>[\w.\-/]+)"
    r"(?P<trailer>\s*#.*)?$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

Resolver = Callable[[str, str, str], str]


def resolve_with_gh(owner: str, repo: str, ref: str) -> str:
    """Resolve a tag or branch to the commit it currently points at."""
    completed = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/commits/{ref}", "--jq", ".sha"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def pin_text(text: str, resolve: Resolver) -> str:
    out: list[str] = []
    for line in text.splitlines():
        match = USES_RE.match(line)
        if match is None or SHA_RE.match(match.group("ref")):
            out.append(line)
            continue
        owner, repo, ref = match.group("owner"), match.group("repo"), match.group("ref")
        sha = resolve(owner, repo, ref)
        if not SHA_RE.match(sha):
            raise ValueError(f"resolver returned a non-sha ref for {owner}/{repo}@{ref}: {sha!r}")
        out.append(
            f"{match.group('indent')}uses: {owner}/{repo}{match.group('subpath')}@{sha}  # {ref}"
        )
    body = "\n".join(out)
    return body + "\n" if text.endswith("\n") else body


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]] or sorted(Path(".github/workflows").glob("*.yml"))
    changed = 0
    for path in paths:
        before = path.read_text(encoding="utf-8")
        after = pin_text(before, resolve_with_gh)
        if after != before:
            path.write_text(after, encoding="utf-8")
            changed += 1
            print(f"pinned {path}")
    print(f"{changed} workflow(s) rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_pin_actions.py -v && .venv/bin/ruff check scripts`
Expected: 7 PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/__init__.py scripts/pin_actions.py tests/unit/test_pin_actions.py
git commit -m "Add a resolver that pins action references to commit shas"
```

---

### Task 7: Workflow hygiene, written before the workflow exists (premortem H35, H36, H1, C9)

This is the test file that makes the whole phase auditable. It applies to **every** workflow in the repository, not just `ci.yml`, so `deploy.yml` and `runpod-reaper.yml` inherit the pinning and least-privilege constraints as failing tests when they land rather than as advice in a spec. It is written first, and it is red, and Task 8 is what turns it green.

**Files:**
- Test: `tests/unit/test_workflow_hygiene.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_workflow_hygiene.py`:
```python
"""Repo-wide GitHub Actions hygiene.

Premortem H35: unpinned third-party Actions (any of which can mint the `gha-deploy` OIDC
token), no per-job `permissions:` block against a repository default of *write*, unpinned base
images defeating SHA traceability.
Premortem H36: `terraform plan` on pull requests is code execution on attacker-supplied `.tf`.
Premortem H1: CI runs bare `pytest`, so the Makefile's PYTHONHASHSEED pin does not apply.
Rubric 4.2: ci.yml on pull requests to main, running a linter and the full test suite.

Note on YAML: `on:` is parsed by PyYAML as the boolean True, because YAML 1.1 treats `on` as a
truthy scalar. Every reader here goes through `triggers()` for that reason.
"""

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(".github/workflows")
CI = WORKFLOWS / "ci.yml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?P<trailer>#.*)?$")
TAG_COMMENT_RE = re.compile(r"#\s*v?\d+[\w.\-/]*")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")
PINNED_RUNNERS = {"ubuntu-24.04-arm", "ubuntu-24.04"}
REQUIRED_CONTEXT = "ci-gate"


def workflow_files() -> list[Path]:
    if not WORKFLOWS.exists():
        return []
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def triggers(doc: dict) -> dict:
    value = doc.get("on", doc.get(True, {}))
    if isinstance(value, list):
        return {key: {} for key in value}
    if isinstance(value, str):
        return {value: {}}
    return value or {}


def jobs(doc: dict) -> dict:
    return doc.get("jobs") or {}


def steps(job: dict) -> list[dict]:
    return [s for s in (job.get("steps") or []) if isinstance(s, dict)]


def run_text(doc: dict) -> str:
    return "\n".join(
        step.get("run", "") for job in jobs(doc).values() for step in steps(job)
    )


# --- the workflows exist at all -------------------------------------------------------

def test_the_ci_workflow_exists():
    assert CI.exists(), "rubric 4.2 names .github/workflows/ci.yml explicitly"


def test_every_workflow_parses():
    for path in workflow_files():
        assert isinstance(load(path), dict), f"{path} is not a YAML mapping"


# --- supply chain, H35 -----------------------------------------------------------------

def test_every_third_party_action_is_pinned_to_a_full_commit_sha():
    offenders = []
    for path in workflow_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = USES_RE.match(line)
            if match is None:
                continue
            ref = match.group("ref").strip("'\"")
            if ref.startswith((".", "docker://")):
                continue
            if "@" not in ref:
                offenders.append(f"{path.name}:{lineno} {ref} (no ref at all)")
                continue
            version = ref.rsplit("@", 1)[1]
            if not SHA_RE.match(version):
                offenders.append(f"{path.name}:{lineno} {ref}")
    assert not offenders, (
        "SHA-pin these actions; a movable tag in ANY job is in the OIDC blast radius "
        "(premortem H35). Run `python -m scripts.pin_actions`:\n  " + "\n  ".join(offenders)
    )


def test_every_pinned_action_keeps_its_tag_as_a_comment():
    """A bare 40-hex string is unmaintainable: nobody can tell v4.2.2 from v3 six weeks later,
    so nobody upgrades it, so the pin rots into an unpatched dependency."""
    offenders = []
    for path in workflow_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = USES_RE.match(line)
            if match is None or match.group("ref").startswith((".", "docker://")):
                continue
            trailer = match.group("trailer") or ""
            if not TAG_COMMENT_RE.search(trailer):
                offenders.append(f"{path.name}:{lineno} {line.strip()}")
    assert not offenders, f"pinned actions with no human-readable version comment: {offenders}"


def test_every_workflow_declares_a_top_level_permissions_block():
    offenders = [p.name for p in workflow_files() if "permissions" not in load(p)]
    assert not offenders, (
        "the repository default GITHUB_TOKEN permission is write; a workflow without a "
        "top-level permissions block inherits it (premortem H35): " + ", ".join(offenders)
    )


def test_every_job_declares_its_own_permissions_block():
    offenders = []
    for path in workflow_files():
        for job_id, job in jobs(load(path)).items():
            if "permissions" not in job:
                offenders.append(f"{path.name}:{job_id}")
    assert not offenders, f"jobs with no explicit permissions: {offenders}"


def test_no_job_grants_write_all_or_a_broad_write():
    allowed_writes = set()  # nothing in this repository needs a write scope on a PR
    offenders = []
    for path in workflow_files():
        for job_id, job in jobs(load(path)).items():
            perms = job.get("permissions")
            if perms in ("write-all", "read-all") or perms is True:
                offenders.append(f"{path.name}:{job_id} -> {perms}")
                continue
            if isinstance(perms, dict):
                for scope, level in perms.items():
                    if level == "write" and (path.name, scope) not in allowed_writes:
                        offenders.append(f"{path.name}:{job_id} -> {scope}: write")
    ci_offenders = [o for o in offenders if o.startswith("ci.yml")]
    assert not ci_offenders, (
        "pull-request CI must hold no write scope at all, including id-token "
        "(premortem H35, H36): " + ", ".join(ci_offenders)
    )


def test_container_images_in_workflows_are_pinned_by_digest():
    offenders = []
    for path in workflow_files():
        for job_id, job in jobs(load(path)).items():
            candidates = []
            container = job.get("container")
            if isinstance(container, str):
                candidates.append(container)
            elif isinstance(container, dict) and "image" in container:
                candidates.append(container["image"])
            for name, service in (job.get("services") or {}).items():
                if isinstance(service, dict) and "image" in service:
                    candidates.append(service["image"])
            for image in candidates:
                if not DIGEST_RE.search(str(image)):
                    offenders.append(f"{path.name}:{job_id} -> {image}")
    assert not offenders, f"pin these images by digest (premortem H35): {offenders}"


def test_runners_are_pinned_labels_not_latest():
    offenders = []
    for path in workflow_files():
        for job_id, job in jobs(load(path)).items():
            runner = job.get("runs-on")
            if isinstance(runner, str) and runner not in PINNED_RUNNERS:
                offenders.append(f"{path.name}:{job_id} -> {runner}")
    assert not offenders, (
        "`ubuntu-latest` silently changes the OS, the preinstalled toolchain, and the CPU "
        "architecture the hashed locks were resolved for: " + ", ".join(offenders)
    )


def test_no_workflow_passes_a_secret_as_a_docker_build_arg():
    """Delivery spec §6.3: a build-arg or ENV bakes a credential into an image layer
    permanently. BuildKit secret mounts are the only sanctioned path."""
    offenders = []
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if re.search(r"(build-args?|--build-arg)", line) and "secrets." in line:
                offenders.append(f"{path.name}:{lineno} {line.strip()}")
    assert not offenders, f"secret passed as a build argument: {offenders}"


# --- no cloud credentials on pull requests, H36 -----------------------------------------

def test_no_workflow_uses_pull_request_target():
    offenders = [p.name for p in workflow_files() if "pull_request_target" in triggers(load(p))]
    assert not offenders, (
        "pull_request_target runs attacker-authored code with the base repository's secrets "
        "and a write token (premortem H36): " + ", ".join(offenders)
    )


def test_ci_never_runs_terraform_plan():
    text = CI.read_text(encoding="utf-8")
    assert "terraform plan" not in text, (
        "`plan` executes provider binaries, `data \"external\"` programs, and module source "
        "fetches against .tf files the pull-request author controls, and the rubric does not "
        "ask for it (premortem H36)"
    )


def test_ci_never_configures_aws_credentials():
    text = CI.read_text(encoding="utf-8")
    for marker in ("configure-aws-credentials", "role-to-assume", "AWS_ACCESS_KEY_ID"):
        assert marker not in text, f"ci.yml reaches for AWS credentials via {marker}"
    for job_id, job in jobs(load(CI)).items():
        perms = job.get("permissions") or {}
        if isinstance(perms, dict):
            assert perms.get("id-token") != "write", (
                f"ci.yml:{job_id} can mint an OIDC token; that is the deploy path, not the "
                "pull-request path (premortem H4, H36)"
            )


def test_terraform_is_validated_without_a_backend_or_state():
    text = run_text(load(CI))
    assert "terraform fmt -check" in text, "rubric-adjacent: formatting drift is a lint failure"
    assert "terraform init -backend=false" in text, (
        "`init` without -backend=false reaches for remote state and therefore credentials"
    )
    assert "terraform validate" in text


# --- rubric 4.2 --------------------------------------------------------------------------

def test_ci_triggers_on_pull_requests_to_main():
    on = triggers(load(CI))
    assert "pull_request" in on, "rubric 4.2: the workflow triggers on pull requests"
    branches = (on.get("pull_request") or {}).get("branches") or []
    assert "main" in branches, f"rubric 4.2: to `main`, not {branches}"


def test_ci_runs_a_linter_and_the_full_test_suite():
    text = run_text(load(CI))
    assert "ruff check" in text, "rubric 4.2 names a linter"
    assert re.search(r"pytest[^\n]*-m\s+[\"']not integration[\"']", text), (
        "the unit half of the suite is not run"
    )
    assert re.search(r"pytest[^\n]*-m\s+integration", text), (
        "rubric 4.1 requires integration tests for the FastAPI endpoints; 'full test suite' "
        "in 4.2 means both halves"
    )


def test_ci_enforces_the_coverage_floor():
    assert "--cov-fail-under=80" in CI.read_text(encoding="utf-8")


def test_ci_sets_pythonhashseed():
    """Premortem H1: the Makefile pinned it, CI ran bare pytest, so the pin did not apply."""
    doc = load(CI)
    workflow_env = doc.get("env") or {}
    if str(workflow_env.get("PYTHONHASHSEED")) == "0":
        return
    for job_id, job in jobs(doc).items():
        if any("pytest" in step.get("run", "") for step in steps(job)):
            job_env = job.get("env") or {}
            assert str(job_env.get("PYTHONHASHSEED")) == "0", (
                f"ci.yml:{job_id} runs pytest without PYTHONHASHSEED=0 (premortem H1)"
            )


def test_ci_installs_the_scanners_it_claims_to_run():
    text = run_text(load(CI))
    assert "gitleaks" in text, "rubric-adjacent QC.1: secret scanning gate"
    assert "semgrep" in text, "rubric-adjacent QC.1: SAST gate"
    assert "run_pip_audit.sh" in text or "pip-audit" in text, "no dependency vulnerability scan"


def test_ci_runs_a_dependency_audit_that_can_fail_the_build():
    """ECR scan-on-push is BASIC scanning: an OS package CVE match that cannot read Python
    distributions and does not fail anything. It is not a dependency scan (premortem H35)."""
    text = run_text(load(CI))
    assert "run_pip_audit.sh" in text
    for job_id, job in jobs(load(CI)).items():
        for step in steps(job):
            if "pip_audit" in step.get("run", "") or "pip-audit" in step.get("run", ""):
                assert not step.get("continue-on-error"), (
                    f"ci.yml:{job_id} audits dependencies and then ignores the result"
                )


def test_no_step_is_marked_continue_on_error():
    offenders = []
    for path in workflow_files():
        for job_id, job in jobs(load(path)).items():
            if job.get("continue-on-error"):
                offenders.append(f"{path.name}:{job_id} (job)")
            for index, step in enumerate(steps(job)):
                if step.get("continue-on-error"):
                    offenders.append(f"{path.name}:{job_id} step {index}")
    assert not offenders, f"a check that cannot fail is not a check: {offenders}"


def test_the_aggregate_gate_job_exists_and_covers_every_other_job():
    doc = load(CI)
    all_jobs = jobs(doc)
    assert REQUIRED_CONTEXT in all_jobs, (
        f"branch protection pins one context, `{REQUIRED_CONTEXT}`; without the aggregate job "
        "every future job silently stops being required"
    )
    gate = all_jobs[REQUIRED_CONTEXT]
    needs = gate.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    missing = sorted(set(all_jobs) - {REQUIRED_CONTEXT} - set(needs))
    assert not missing, f"{REQUIRED_CONTEXT} does not depend on: {missing}"
    assert str(gate.get("if")).strip() == "always()", (
        "without `if: always()` a skipped upstream job skips the gate, and a skipped required "
        "check reports as pending forever"
    )
    body = "\n".join(step.get("run", "") for step in steps(gate))
    assert "needs" in str(gate) and "exit 1" in body, (
        "the gate must inspect every upstream result and exit non-zero on anything but success"
    )


def test_the_gate_job_name_matches_the_protected_context():
    gate = jobs(load(CI))[REQUIRED_CONTEXT]
    assert gate.get("name", REQUIRED_CONTEXT) == REQUIRED_CONTEXT, (
        "the required status check is matched by the check-run name, which is the job's `name` "
        "when set; a mismatch deadlocks every merge on a context that never reports"
    )


# --- the scheduled reaper, if it survived the cut-line ------------------------------------

def test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded():
    reaper = WORKFLOWS / "runpod-reaper.yml"
    if not reaper.exists():
        log = Path("docs/cut-log.md")
        assert log.exists() and "runpod-reaper" in log.read_text(encoding="utf-8"), (
            "the reaper is absent and no cut is recorded; a GPU pod left running is the one "
            "cost failure with no ceiling. Record the cut in docs/cut-log.md or restore it"
        )
        return
    on = triggers(load(reaper))
    assert "schedule" in on, "the reaper only works if something runs it"
    assert "workflow_dispatch" in on, (
        "a reaper you cannot trigger by hand is a reaper you cannot use during an incident"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_workflow_hygiene.py -v`
Expected: FAIL — `test_the_ci_workflow_exists` with `AssertionError: rubric 4.2 names .github/workflows/ci.yml explicitly`, and every test that reads `CI` erroring with `FileNotFoundError`. That is the correct red: the file the rubric names does not exist yet.

- [ ] **Step 3: Write minimal implementation**

None. This task ships the specification as executable assertions; Task 8 writes the artifact that satisfies them. Committing a red test file on its own branch commit is deliberate — it is the record of what the gate was required to do, separate from the YAML that happens to do it.

- [ ] **Step 4: Confirm the failure is the expected one, not a broken test**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_workflow_hygiene.py -v --tb=line 2>&1 | tail -25`
Expected: the failures are `AssertionError` / `FileNotFoundError` on the missing `ci.yml`, and **no** `NameError`, `AttributeError`, or `yaml` parse error, which would mean the test file itself is wrong.

Also verify the YAML `on:` trap is handled, since a silent failure there would make four rubric assertions vacuous:
```bash
PYTHONHASHSEED=0 .venv/bin/python -c "
import yaml
doc = yaml.safe_load('on:\n  pull_request:\n    branches: [main]\n')
print('parsed keys:', list(doc))
assert True in doc, 'PyYAML no longer coerces on: to True; simplify triggers()'
print('the True-key workaround is still required')
"
```
Expected: `parsed keys: [True]` then `the True-key workaround is still required`.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_workflow_hygiene.py
git commit -m "Add repo-wide workflow supply-chain and permissions assertions"
```

---

### Task 8: `ci.yml` — the gate itself (rubric 4.2, premortem H35, H36, C9)

**Files:**
- Create: `.github/workflows/ci.yml`, `.terraform-version`, `scripts/install_gitleaks.sh`, `scripts/gitleaks.sha256`
- Test: `tests/unit/test_workflow_hygiene.py` (from Task 7, turns green)

- [ ] **Step 1: The failing test already exists**

Task 7's suite is the failing test for this task. Confirm it is still red before writing anything:

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_workflow_hygiene.py -q`
Expected: FAIL, dominated by `test_the_ci_workflow_exists`.

- [ ] **Step 2: Resolve the three values that must not be transcribed by hand**

Nothing fabricated ever enters the repository. Each of these is produced by a command:

```bash
# 1. The Postgres service-container digest, for the multi-arch tag this project uses.
docker buildx imagetools inspect postgres:16-alpine --format '{{ .Manifest.Digest }}'

# 2. The Terraform version, taken from the binary this project already validated against.
terraform version -json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['terraform_version'])" \
  > .terraform-version
cat .terraform-version

# 3. The gitleaks release checksums, straight from the pinned release.
mkdir -p scripts
curl --fail --silent --show-error --location \
  https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_checksums.txt \
  | grep -E 'linux_(arm64|x64)\.tar\.gz$' > scripts/gitleaks.sha256
cat scripts/gitleaks.sha256
```
Expected: a `sha256:`-prefixed 64-hex digest; a version string such as `1.15.8`; and two `<sha256>  gitleaks_8.21.2_linux_<arch>.tar.gz` lines.

- [ ] **Step 3: Write minimal implementation**

`scripts/install_gitleaks.sh` (`chmod +x`):
```bash
#!/usr/bin/env bash
# Install gitleaks from a release tarball whose checksum is committed to this repository.
#
# The alternative — a third-party Docker action — adds a program that reads the entire working
# tree to the set of things that can mint an OIDC token in some other job (premortem H35). A
# scanner should be under the same integrity rule as the code it scans.
set -euo pipefail

VERSION="8.21.2"
CHECKSUMS="scripts/gitleaks.sha256"

case "$(uname -m)" in
  aarch64|arm64) ARCH="arm64" ;;
  x86_64)        ARCH="x64" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

TARBALL="gitleaks_${VERSION}_linux_${ARCH}.tar.gz"
URL="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${TARBALL}"

EXPECTED="$(awk -v want="${TARBALL}" '$2 == want || $2 == "*" want { print $1 }' "${CHECKSUMS}")"
if [ -z "${EXPECTED}" ]; then
  echo "no committed checksum for ${TARBALL} in ${CHECKSUMS}" >&2
  exit 1
fi

mkdir -p bin
curl --fail --silent --show-error --location --output "/tmp/${TARBALL}" "${URL}"
if ! echo "${EXPECTED}  /tmp/${TARBALL}" | sha256sum --check --status; then
  echo "checksum mismatch for ${TARBALL}; refusing to run it" >&2
  exit 1
fi

tar -xzf "/tmp/${TARBALL}" -C bin gitleaks
chmod +x bin/gitleaks
bin/gitleaks version
```

`.github/workflows/ci.yml`. Write it with version tags, then pin in the next command — so the committed file has real SHAs produced by `gh`, and no placeholder is ever committed:
```yaml
name: ci

# Rubric 4.2: "a GitHub Actions workflow that triggers on pull requests to main".
# `pull_request` (not `pull_request_target`) gives a fork's PR a read-only GITHUB_TOKEN and no
# access to repository secrets, which is what keeps a fork out of the deploy path.
on:
  pull_request:
    branches: [main]

# The repository default token permission is write. Denying everything here and re-granting
# per job is the only configuration where adding a job cannot silently widen the token.
permissions: {}

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

env:
  PYTHONHASHSEED: "0"
  PIP_DISABLE_PIP_VERSION_CHECK: "1"

jobs:
  lint:
    name: lint
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5.3.0
        with:
          python-version: "3.11"
      - name: Install the hashed dev lock
        run: python -m pip install --require-hashes -r requirements/dev.txt
      - name: ruff
        run: ruff check .

  test:
    name: test
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: toxic_test
        ports:
          - 5433:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      TEST_DATABASE_URL: postgresql+psycopg://postgres:postgres@localhost:5433/toxic_test
    steps:
      - uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5.3.0
        with:
          python-version: "3.11"
      - uses: actions/cache@v4.1.2
        with:
          path: ~/.cache/pip
          key: pip-${{ runner.os }}-${{ runner.arch }}-${{ hashFiles('requirements/*.txt') }}
      - name: Install the hashed dev lock
        run: python -m pip install --require-hashes -r requirements/dev.txt
      - name: Unit tests
        run: pytest -m "not integration" --cov --cov-report=
      - name: Integration tests against a real Postgres
        run: pytest -m integration --cov --cov-append --cov-report=term-missing:skip-covered --cov-fail-under=80

  secrets-scan:
    name: secrets-scan
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4.2.2
        with:
          fetch-depth: 0
          persist-credentials: false
      - name: Install gitleaks from a checksum-verified release
        run: ./scripts/install_gitleaks.sh
      - name: gitleaks
        run: ./bin/gitleaks detect --source . --redact --no-banner --exit-code 1

  sast:
    name: sast
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5.3.0
        with:
          python-version: "3.11"
      - name: Install the hashed security lock
        run: python -m pip install --require-hashes -r requirements/security.txt
      - name: semgrep
        env:
          SEMGREP_SEND_METRICS: "off"
        run: semgrep scan --config p/python --config p/secrets --error --metrics=off --disable-version-check .

  deps-audit:
    name: deps-audit
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5.3.0
        with:
          python-version: "3.11"
      - name: Install the hashed security lock
        run: python -m pip install --require-hashes -r requirements/security.txt
      - name: Audit every shipped lock
        run: ./scripts/run_pip_audit.sh

  terraform:
    name: terraform
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v4.2.2
        with:
          persist-credentials: false
      - name: Read the pinned Terraform version
        id: tfver
        run: echo "version=$(cat .terraform-version)" >> "$GITHUB_OUTPUT"
      - uses: hashicorp/setup-terraform@v3.1.2
        with:
          terraform_version: ${{ steps.tfver.outputs.version }}
          terraform_wrapper: false
      - name: Refuse to pass silently when there is nothing to validate
        run: |
          test -d infra/terraform || { echo "::error::infra/terraform is missing"; exit 1; }
          test -n "$(find infra/terraform -name '*.tf' -print -quit)" \
            || { echo "::error::infra/terraform contains no .tf files"; exit 1; }
      - name: terraform fmt
        run: terraform fmt -check -recursive infra/
      - name: terraform validate
        working-directory: infra/terraform
        run: |
          terraform init -backend=false -input=false
          terraform validate

  ci-gate:
    name: ci-gate
    needs: [lint, test, secrets-scan, sast, deps-audit, terraform]
    if: always()
    runs-on: ubuntu-24.04-arm
    permissions: {}
    steps:
      - name: Fail unless every required job succeeded
        env:
          RESULTS: ${{ join(needs.*.result, ' ') }}
        run: |
          echo "upstream results: ${RESULTS}"
          for result in ${RESULTS}; do
            if [ "${result}" != "success" ]; then
              echo "::error::a required job reported '${result}'"
              exit 1
            fi
          done
          echo "all required jobs succeeded"
```

Now pin the actions and the service image with real values:
```bash
PYTHONHASHSEED=0 .venv/bin/python -m scripts.pin_actions .github/workflows/ci.yml

DIGEST=$(docker buildx imagetools inspect postgres:16-alpine --format '{{ .Manifest.Digest }}')
python3 - "$DIGEST" <<'PY'
import sys
from pathlib import Path

digest = sys.argv[1]
path = Path(".github/workflows/ci.yml")
text = path.read_text(encoding="utf-8")
assert "image: postgres:16-alpine\n" in text, "the service image line moved; pin it by hand"
path.write_text(
    text.replace("image: postgres:16-alpine\n", f"image: postgres:16-alpine@{digest}\n"),
    encoding="utf-8",
)
print(f"pinned postgres:16-alpine at {digest}")
PY

chmod +x scripts/install_gitleaks.sh
grep -n "uses:" .github/workflows/ci.yml
```
Expected: `pinned <path>` from the pinner, `pinned postgres:16-alpine at sha256:...`, and every `uses:` line showing a 40-hex SHA followed by `  # v<tag>`.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_workflow_hygiene.py tests/unit/test_install_commands.py -v`
Expected: all workflow-hygiene tests PASS except `test_ci_runs_a_dependency_audit_that_can_fail_the_build` and `test_ci_installs_the_scanners_it_claims_to_run`, which stay red until Task 9 adds `scripts/run_pip_audit.sh`. That is the intended seam; do not stub the script to make them green.

Then prove the gate actually bites rather than merely parsing, by running the same commands the runner will:
```bash
.venv/bin/ruff check .
./scripts/install_gitleaks.sh && ./bin/gitleaks detect --source . --redact --no-banner --exit-code 1
```
Expected: ruff clean; gitleaks reports `no leaks found` and exits 0.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml .terraform-version scripts/install_gitleaks.sh scripts/gitleaks.sha256
git commit -m "Add the pull request CI gate with SHA-pinned actions and per-job permissions"
```

---

### Task 9: A dependency scan that fails the build, with a suppression ledger that expires (premortem H35)

The AWS foundation spec enables ECR scan-on-push. That is Amazon ECR **basic** scanning: a CVE match against the operating-system package database. It cannot read Python distributions, so scikit-learn, FastAPI, streamlit, SQLAlchemy and their entire transitive closure are invisible to it, and it fails nothing — it writes a report. This task adds the scan that actually covers the shipped dependency set and actually blocks a merge, plus the one control that keeps such a scan honest over nineteen days: a suppression ledger where every entry carries a reason and an expiry date, and an expired entry fails the build.

**Files:**
- Create: `scripts/vuln_ledger.py`, `scripts/run_pip_audit.sh`, `docs/security/pip-audit-ignores.md`
- Modify: `Makefile`
- Test: `tests/unit/test_vuln_ledger.py`

**Interfaces produced:** `Suppression`, `parse_ledger`, `active_ids`, `expired`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_vuln_ledger.py`:
```python
"""The pip-audit suppression ledger (premortem H35).

A scanner with an unbounded ignore list converges on ignoring everything, because the cheapest
response to a red build at 1 a.m. is one more line in the ignore list. Every suppression here
carries a reason and an expiry, and an expired suppression fails the build — which forces the
decision to be re-made rather than inherited.
"""

import datetime as dt
from pathlib import Path

import pytest

from scripts.vuln_ledger import active_ids, expired, parse_ledger

LEDGER = Path("docs/security/pip-audit-ignores.md")

SAMPLE = """
| Vulnerability | Package | Reason it is not exploitable here | Expires |
|---|---|---|---|
| GHSA-aaaa-bbbb-cccc | urllib3 | Proxy-only code path; this project sets no proxy | 2026-09-30 |
| PYSEC-2026-1 | jinja2 | Sandbox escape; no user-controlled template is rendered | 2026-08-01 |
"""


def test_parse_ledger_reads_id_package_reason_and_expiry():
    rows = parse_ledger(SAMPLE)
    assert [row.vuln_id for row in rows] == ["GHSA-aaaa-bbbb-cccc", "PYSEC-2026-1"]
    assert rows[0].package == "urllib3"
    assert rows[0].expires == dt.date(2026, 9, 30)
    assert "proxy" in rows[0].reason.lower()


def test_parse_ledger_ignores_the_header_and_separator_rows():
    assert len(parse_ledger(SAMPLE)) == 2


def test_a_suppression_with_no_reason_is_rejected():
    bad = SAMPLE.replace("Proxy-only code path; this project sets no proxy", "  ")
    with pytest.raises(ValueError, match="reason"):
        parse_ledger(bad)


def test_a_suppression_with_an_unparseable_expiry_is_rejected():
    bad = SAMPLE.replace("2026-09-30", "soon")
    with pytest.raises(ValueError, match="expiry"):
        parse_ledger(bad)


def test_a_suppression_with_no_expiry_is_rejected():
    bad = SAMPLE.replace("| 2026-09-30 |", "|  |")
    with pytest.raises(ValueError, match="expiry"):
        parse_ledger(bad)


def test_expired_suppressions_are_reported_and_not_applied():
    rows = parse_ledger(SAMPLE)
    today = dt.date(2026, 8, 15)
    assert [row.vuln_id for row in expired(rows, today)] == ["PYSEC-2026-1"]
    assert active_ids(rows, today) == ["GHSA-aaaa-bbbb-cccc"]


def test_the_committed_ledger_parses_and_has_no_expired_row():
    assert LEDGER.exists(), "docs/security/pip-audit-ignores.md is missing"
    rows = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    stale = expired(rows, dt.date.today())
    assert not stale, (
        "these suppressions have outlived their justification and must be re-decided: "
        + ", ".join(f"{row.vuln_id} ({row.package}, expired {row.expires})" for row in stale)
    )


def test_the_audit_script_reads_the_ledger_rather_than_hardcoding_ignores():
    script = Path("scripts/run_pip_audit.sh").read_text(encoding="utf-8")
    assert "vuln_ledger" in script, "the ignore list must come from the reviewed ledger"
    assert "--ignore-vuln" not in script.split("vuln_ledger")[0], (
        "a hardcoded --ignore-vuln bypasses the ledger and its expiry rule"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_vuln_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.vuln_ledger'` (collection error on all eight tests).

- [ ] **Step 3: Write minimal implementation**

`scripts/vuln_ledger.py`:
```python
"""Parse the pip-audit suppression ledger.

Premortem H35 asks for a dependency scan that fails the build. The failure mode of such a scan
is not that it misses something; it is that a growing ignore list quietly turns it off. Every
row here needs a reason a human wrote and a date after which the decision must be re-made.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

VULN_ID_RE = re.compile(r"^(GHSA-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}|PYSEC-\d{4}-\d+)$")


@dataclass(frozen=True)
class Suppression:
    vuln_id: str
    package: str
    reason: str
    expires: dt.date


def parse_ledger(text: str) -> list[Suppression]:
    rows: list[Suppression] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4:
            continue
        vuln_id, package, reason, expires = cells
        if not VULN_ID_RE.match(vuln_id):
            continue  # header row, separator row, or prose
        if not reason:
            raise ValueError(f"{vuln_id} has no reason; a suppression without one is a habit")
        if not package:
            raise ValueError(f"{vuln_id} names no package")
        try:
            parsed = dt.date.fromisoformat(expires)
        except ValueError as exc:
            raise ValueError(
                f"{vuln_id} has an unusable expiry {expires!r}; use YYYY-MM-DD"
            ) from exc
        rows.append(Suppression(vuln_id, package, reason, parsed))
    return rows


def expired(ledger: list[Suppression], today: dt.date) -> list[Suppression]:
    return [row for row in ledger if row.expires < today]


def active_ids(ledger: list[Suppression], today: dt.date) -> list[str]:
    return [row.vuln_id for row in ledger if row.expires >= today]


def main() -> int:
    """Print the active ignore ids, one per line, for scripts/run_pip_audit.sh."""
    from pathlib import Path

    ledger = parse_ledger(Path("docs/security/pip-audit-ignores.md").read_text(encoding="utf-8"))
    today = dt.date.today()
    stale = expired(ledger, today)
    if stale:
        for row in stale:
            print(
                f"expired suppression {row.vuln_id} ({row.package}) lapsed {row.expires}",
                file=__import__("sys").stderr,
            )
        return 1
    for vuln_id in active_ids(ledger, today):
        print(vuln_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`docs/security/pip-audit-ignores.md`:
```markdown
# pip-audit suppressions

`scripts/run_pip_audit.sh` reads this table and passes each active row to `pip-audit` as
`--ignore-vuln`. `tests/unit/test_vuln_ledger.py` fails the build if a row has no reason, has
no parseable expiry date, or has expired.

Rules, in the order they matter:

1. A suppression is a decision about **this** project, not about the advisory. The reason must
   say why the vulnerable code path is unreachable here, not that a fix is unavailable.
2. Expiry is at most 30 days out. On day 20 of a 19-day project, an expiry beyond the due date
   is a decision nobody will ever revisit.
3. Upgrading is always preferred. Add a row only when the fixed version is not installable —
   for example when it requires a Python this project does not run.

| Vulnerability | Package | Reason it is not exploitable here | Expires |
|---|---|---|---|
```

`scripts/run_pip_audit.sh` (`chmod +x`):
```bash
#!/usr/bin/env bash
# Audit every dependency surface this project actually ships.
#
# ECR scan-on-push is BASIC scanning: an OS-package CVE match that cannot read Python
# distributions and does not fail a build (premortem H35). This is the scan that covers the
# dependency set and blocks the merge.
set -euo pipefail

LOCKS=(
  requirements/base.txt
  requirements/serve.txt
  requirements/ui.txt
  requirements/monitor.txt
  requirements/dev.txt
)

mapfile -t IGNORED < <(python -m scripts.vuln_ledger)

IGNORE_ARGS=()
for vuln in "${IGNORED[@]}"; do
  [ -n "${vuln}" ] || continue
  echo "suppressed by the reviewed ledger: ${vuln}"
  IGNORE_ARGS+=(--ignore-vuln "${vuln}")
done

status=0
for lock in "${LOCKS[@]}"; do
  [ -f "${lock}" ] || continue
  echo "::group::pip-audit ${lock}"
  # --no-deps: the lock IS the full resolved closure, so no network resolution is needed and
  # nothing outside the audited set can be pulled in.
  # --strict: fail if any listed distribution could not be audited, rather than skipping it.
  pip-audit --strict --no-deps --progress-spinner=off -r "${lock}" "${IGNORE_ARGS[@]}" || status=1
  echo "::endgroup::"
done

exit "${status}"
```

Append to the `Makefile`:
```makefile
.PHONY: scan
# The same three commands CI runs, so a red gate is reproducible locally in one step.
scan:
	./scripts/install_gitleaks.sh
	./bin/gitleaks detect --source . --redact --no-banner --exit-code 1
	$(BIN)/python -m pip install --require-hashes -r requirements/security.txt
	$(BIN)/semgrep scan --config p/python --config p/secrets --error --metrics=off --disable-version-check .
	./scripts/run_pip_audit.sh
```

Then wire the audit into `ci.yml` — it is already there as the `deps-audit` job's `Audit every shipped lock` step, so no workflow edit is needed. Confirm:
```bash
chmod +x scripts/run_pip_audit.sh
grep -n "run_pip_audit" .github/workflows/ci.yml
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_vuln_ledger.py tests/unit/test_workflow_hygiene.py -v`
Expected: 8 PASS in the ledger file, and the two workflow tests left red by Task 8 (`test_ci_installs_the_scanners_it_claims_to_run`, `test_ci_runs_a_dependency_audit_that_can_fail_the_build`) now PASS. The whole workflow-hygiene suite is green.

Then run the real audit:
```bash
.venv/bin/python -m pip install --require-hashes -r requirements/security.txt
./scripts/run_pip_audit.sh
```
Expected: one `::group::pip-audit requirements/<surface>.txt` block per lock and `No known vulnerabilities found`, exit 0. **If a vulnerability is reported, upgrade the pin in the relevant `.in`, re-run `make lock`, and re-run the audit.** Only add a ledger row when the fixed version genuinely cannot be installed, and say why in the reason column.

Finally, prove the ledger's expiry rule bites rather than being decorative:
```bash
printf '| GHSA-aaaa-bbbb-cccc | urllib3 | temporary, deliberately stale | 2020-01-01 |\n' \
  >> docs/security/pip-audit-ignores.md
PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_vuln_ledger.py::test_the_committed_ledger_parses_and_has_no_expired_row -q
git checkout docs/security/pip-audit-ignores.md
```
Expected: the test FAILS with `these suppressions have outlived their justification`, then the file is restored.

- [ ] **Step 5: Commit**

```bash
git add scripts/vuln_ledger.py scripts/run_pip_audit.sh docs/security/pip-audit-ignores.md Makefile tests/unit/test_vuln_ledger.py
git commit -m "Add a dependency vulnerability gate with an expiring suppression ledger"
```

---

### Task 10: Base images pinned by digest, repo-wide (premortem H35)

Phase 2 pinned `backend/Dockerfile` and tested it in isolation. Three more images land in Phases 3 and 5, on the instances that face the internet, and each one has to be pinned by the same rule. A tag is a mutable pointer: `python:3.11-slim-bookworm` in June and in August are different filesystems, which silently breaks the property that a deployed container traces back to an exact commit.

**Files:**
- Test: `tests/unit/test_image_pinning.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_image_pinning.py`:
```python
"""Every container image this project builds or runs is pinned by digest (premortem H35).

`FROM python:3.11-slim-bookworm` resolves to a different filesystem every few weeks. Image tags
are immutable in ECR by design in this project, and that guarantee is worthless if the base the
image was built FROM is not.
"""

import re
from pathlib import Path

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")
FROM_RE = re.compile(r"^\s*FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<stage>\S+))?\s*$", re.IGNORECASE)
SKIP_PARTS = {".venv", "build", "node_modules", ".git", "__pycache__"}


def dockerfiles() -> list[Path]:
    found = [
        path
        for path in Path(".").rglob("Dockerfile*")
        if path.is_file() and not SKIP_PARTS & set(path.parts)
    ]
    return sorted(found)


def compose_files() -> list[Path]:
    return sorted(
        path
        for path in Path("infra").rglob("*compose*.y*ml")
        if path.is_file() and not SKIP_PARTS & set(path.parts)
    )


def test_the_scanner_finds_the_dockerfiles_the_project_has():
    assert dockerfiles(), "no Dockerfile found; rubric 5.1 requires containerized components"


def test_every_dockerfile_base_image_is_pinned_by_digest():
    offenders = []
    for path in dockerfiles():
        stages: set[str] = set()
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = FROM_RE.match(line)
            if match is None:
                continue
            image = match.group("image")
            if match.group("stage"):
                stages.add(match.group("stage"))
            if image in stages or image == "scratch":
                continue  # an earlier stage in the same file, or the empty base
            if not DIGEST_RE.search(image):
                offenders.append(f"{path}:{lineno} FROM {image}")
    assert not offenders, (
        "pin these base images by digest; resolve one with "
        "`docker buildx imagetools inspect <image> --format '{{ .Manifest.Digest }}'`:\n  "
        + "\n  ".join(offenders)
    )


def test_every_compose_service_image_is_pinned_by_digest():
    offenders = []
    for path in compose_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            image = stripped.split(":", 1)[1].strip().strip("'\"")
            if "${" in image:
                continue  # ECR image resolved at deploy time by immutable git-sha tag
            if not DIGEST_RE.search(image):
                offenders.append(f"{path}:{lineno} {image}")
    assert not offenders, f"pin these compose images by digest: {offenders}"


def test_no_dockerfile_runs_as_root_at_the_end():
    """Not H35, but the same one-line class of omission, and it is free to assert once a test
    file is walking every Dockerfile anyway."""
    offenders = []
    for path in dockerfiles():
        users = re.findall(r"^\s*USER\s+(\S+)", path.read_text(encoding="utf-8"), re.MULTILINE)
        if not users or users[-1] in {"root", "0"}:
            offenders.append(str(path))
    assert not offenders, f"these images run as root: {offenders}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_image_pinning.py -v`
Expected: FAIL — `test_every_dockerfile_base_image_is_pinned_by_digest` naming `frontend/Dockerfile`, `monitoring/Dockerfile`, and `rescorer/Dockerfile` (whichever Phase 3 produced), and `test_every_compose_service_image_is_pinned_by_digest` naming `infra/docker-compose.yml:<n> postgres:16-alpine`. `backend/Dockerfile` passes, because Phase 2 pinned it.

- [ ] **Step 3: Write minimal implementation**

Resolve every base and rewrite in place, so no digest is ever transcribed:
```bash
python3 - <<'PY'
import re
import subprocess
from pathlib import Path

DIGEST = re.compile(r"@sha256:[0-9a-f]{64}")
FROM = re.compile(r"^(?P<head>\s*FROM\s+)(?P<image>\S+)(?P<tail>.*)$", re.IGNORECASE)
SKIP = {".venv", "build", "node_modules", ".git", "__pycache__"}


def resolve(image: str) -> str:
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{ .Manifest.Digest }}"],
        capture_output=True, text=True, check=True,
    )
    digest = out.stdout.strip()
    assert digest.startswith("sha256:") and len(digest) == 71, digest
    return digest


for path in sorted(Path(".").rglob("Dockerfile*")):
    if not path.is_file() or SKIP & set(path.parts):
        continue
    stages, changed, lines = set(), False, []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = FROM.match(line)
        if match:
            image = match.group("image")
            stage = re.search(r"\bAS\s+(\S+)", match.group("tail"), re.IGNORECASE)
            if stage:
                stages.add(stage.group(1))
            if image not in stages and image != "scratch" and not DIGEST.search(image):
                line = f"{match.group('head')}{image}@{resolve(image)}{match.group('tail')}"
                changed = True
        lines.append(line)
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"pinned {path}")
PY

# The local compose Postgres, which the integration suite runs against.
DIGEST=$(docker buildx imagetools inspect postgres:16-alpine --format '{{ .Manifest.Digest }}')
python3 - "$DIGEST" <<'PY'
import sys
from pathlib import Path

digest = sys.argv[1]
for path in sorted(Path("infra").rglob("*compose*.y*ml")):
    text = path.read_text(encoding="utf-8")
    updated = text.replace("image: postgres:16-alpine\n", f"image: postgres:16-alpine@{digest}\n")
    if updated != text:
        path.write_text(updated, encoding="utf-8")
        print(f"pinned {path}")
PY
```

If `test_no_dockerfile_runs_as_root_at_the_end` reports an image, add a non-root user to it, matching the pattern Phase 2 already uses in `backend/Dockerfile`:
```dockerfile
RUN useradd --create-home --uid 10001 appuser
USER appuser
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_image_pinning.py -v`
Expected: 4 PASS.

Then confirm the pinned bases still build and the local stack still comes up:
```bash
docker compose -f infra/docker-compose.yml config >/dev/null && echo "compose parses"
make db-up && make test-integration
```
Expected: `compose parses`, Postgres becomes ready, and the integration suite is green against the digest-pinned database.

- [ ] **Step 5: Commit**

```bash
git add $(git diff --name-only) tests/unit/test_image_pinning.py
git commit -m "Pin every container base image and compose service by digest"
```

---

### Task 11: Executable interface-contract conformance (premortem H24)

The master plan's Interface Contracts block says "Names and types here are authoritative; phase files must match them exactly," and it has drifted in five places because the hardening commit updated the code and not the contract. A phase implementer sees only their own phase file plus that block, so a stale contract is a wrong instruction delivered with authority. Phase 4 is the only phase that can see all the seams at once, so it is where the reconciliation becomes a test instead of a promise.

**Files:**
- Modify: `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`
- Test: `tests/unit/test_interface_contracts.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_interface_contracts.py`:
```python
"""The master plan's Interface Contracts block is executable (premortem H24).

It declares itself authoritative across phases, and it drifted in five places because the
hardening commit changed the code without changing the contract. Contract definitions must be
written on ONE line each so this parser can read them; that constraint is cheap and it is what
makes the block checkable at all.
"""

import importlib
import inspect
import re
from pathlib import Path

MASTER_PLAN = Path("docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md")
FENCE = "`" * 3  # built, not written literally, so this file can live inside a fenced block
MODULE_RE = re.compile(r"^#\s*(?P<path>[\w/]+\.py)\b")
CLASS_RE = re.compile(r"^class\s+(?P<name>\w+)")
SUPERSEDED = (
    "sha256 over sorted deduped ids",
    'config: SplitConfig) -> DatasetBundle',
    "about 60 rows",
    "tf plan gate on PR",
    # Phase 0 v2 Task 14 split the composite into three fields. A contract row that still
    # declares the single opaque field is a wrong instruction delivered with authority.
    "data_version: str                 # sha256 over",
)
REQUIRED = (
    'Literal["allow", "review", "block"]',
    "probs_to_dict",
    "write_pending",
    "init_db",
    # Phase 0 v2 Task 18 wrote this block. Phase 4 verifies it; it does not rewrite it.
    "config: SplitConfig = DEFAULT_SPLIT",
    "raw_sha256",
    "split_version",
    "env_version",
    "normalize_for_serving",
    "def make_splits(",
)


def contracts_section() -> str:
    text = MASTER_PLAN.read_text(encoding="utf-8")
    start = text.index("## Interface Contracts")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def python_blocks(section: str) -> list[str]:
    return re.findall(rf"{FENCE}python\n(.*?){FENCE}", section, re.S)


def split_params(raw: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in raw:
        if char in "[({":
            depth += 1
        elif char in "])}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parse_def(line: str) -> tuple[str, list[str]] | None:
    match = re.match(r"^def\s+(\w+)\(", line)
    if match is None:
        return None
    depth, start = 0, line.index("(")
    for index in range(start, len(line)):
        if line[index] in "([{":
            depth += 1
        elif line[index] in ")]}":
            depth -= 1
            if depth == 0:
                names = [
                    param.split(":")[0].split("=")[0].strip().lstrip("*")
                    for param in split_params(line[start + 1 : index])
                ]
                return match.group(1), [name for name in names if name]
    return None


def declared_symbols() -> list[tuple[str, str, list[str] | None]]:
    found: list[tuple[str, str, list[str] | None]] = []
    for block in python_blocks(contracts_section()):
        module: str | None = None
        for line in block.splitlines():
            header = MODULE_RE.match(line.strip())
            if header:
                module = header.group("path")[:-3].replace("/", ".")
                continue
            if module is None or line[:1].isspace():
                continue  # methods and fields belong to the class above them
            parsed = parse_def(line)
            if parsed:
                found.append((module, parsed[0], parsed[1]))
                continue
            klass = CLASS_RE.match(line)
            if klass:
                found.append((module, klass.group("name"), None))
    return found


def test_the_contracts_block_declares_something_parseable():
    symbols = declared_symbols()
    assert len(symbols) >= 10, f"the contracts parser found only {len(symbols)} symbols"
    assert ("model.contract", "probs_to_dict", ["row"]) in symbols


def test_every_contract_symbol_exists_with_the_declared_parameters():
    problems = []
    for module_path, name, params in declared_symbols():
        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            problems.append(f"{module_path}: not importable ({exc})")
            continue
        target = getattr(module, name, None)
        if target is None:
            problems.append(f"{module_path}.{name}: declared in the contract, absent in code")
            continue
        if params is None:
            continue
        actual = [
            p for p in inspect.signature(target).parameters if p not in {"self", "cls"}
        ]
        if actual != params:
            problems.append(f"{module_path}.{name}: contract {params} != code {actual}")
    assert not problems, "the authoritative contract block does not match the code:\n  " + "\n  ".join(problems)


def test_the_contracts_block_carries_no_superseded_text():
    text = MASTER_PLAN.read_text(encoding="utf-8")
    stale = [phrase for phrase in SUPERSEDED if phrase in text]
    assert not stale, f"pre-hardening text still in the authoritative plan (H24): {stale}"


def test_the_contracts_block_carries_the_corrected_text():
    text = MASTER_PLAN.read_text(encoding="utf-8")
    missing = [phrase for phrase in REQUIRED if phrase not in text]
    assert not missing, f"the corrections were not applied (H24): {missing}"


def test_there_is_exactly_one_interface_contract_conformance_suite():
    """H24, one layer down. Phase 0 v2 Task 18 wrote
    tests/unit/test_interface_contract_doc.py and this file was written independently;
    both assert the contents of the SAME section of the SAME document, in mutually
    exclusive ways. Two conformance suites for one contract is how the contract acquires
    two meanings and both pass locally."""
    suites = sorted(p.name for p in Path("tests/unit").glob("test_interface_contract*.py"))
    assert suites == ["test_interface_contracts.py"], (
        f"{suites}: keep one suite. Phase 0's cases were merged into this file; delete "
        "tests/unit/test_interface_contract_doc.py rather than maintaining two."
    )


def test_probs_to_dict_is_defined_exactly_once():
    """H23 recurring inside the remediation for H23. Phase 0 Task 12, Phase 1 Task 1 and
    Phase 2 Task 1 each say 'Append to model/contract.py' and each ship a DIFFERENT body
    with a DIFFERENT error message. Python keeps the last def, so whichever phase lands
    last silently redefines the adapter for the two that landed earlier, and the earlier
    phases' `pytest.raises(match=...)` cases go red without anyone touching them."""
    source = Path("model/contract.py").read_text(encoding="utf-8")
    assert source.count("def probs_to_dict(") == 1, "the adapter was redefined (H23)"


def test_the_canonical_adapter_raises_both_documented_messages():
    """Pins the ONE body all three phases' tests must be written against."""
    import numpy as np
    import pytest as _pytest

    from model.contract import probs_to_dict

    with _pytest.raises(ValueError, match="1-D"):
        probs_to_dict(np.zeros((2, 6)))
    with _pytest.raises(ValueError, match="expected 6 probabilities"):
        probs_to_dict(np.zeros(5))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_interface_contracts.py -v`
Expected: FAIL — `test_the_contracts_block_carries_no_superseded_text` with `AssertionError: pre-hardening text still in the authoritative plan (H24): ['sha256 over sorted deduped ids', 'config: SplitConfig) -> DatasetBundle', 'about 60 rows', 'tf plan gate on PR']`, and `test_every_contract_symbol_exists_with_the_declared_parameters` reporting `backend.db.insert_prediction: declared in the contract, absent in code`.

- [ ] **Step 3: Write minimal implementation**

Apply five edits to `docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md`. The code is right; the contract is stale, so the contract moves.

**Edit 1 — dataset preparation. DO NOT REWRITE THIS BLOCK. Verify it.**

> **Correction, 2026-07-31 (H24, one layer down).** As originally written, this edit replaced the `DatasetBundle` block with a **four-field, single-`data_version`** form — reverting the very correction Phase 0 v2 Task 18 makes. Both tasks rewrite the same section of the same document, incompatibly, and both ship a conformance suite asserting its own version. The consequence, with nobody touching either file again: Phase 0's `test_dataset_bundle_fields_match_the_documented_block` goes red (documented four fields vs a live seven-field dataclass), Phase 0's `test_documented_bundle_no_longer_carries_the_old_data_version_field` goes red, Phase 0's `test_documented_fixture_size_matches_the_committed_fixture` goes red against "36 rows", and this file's `test_the_contracts_block_carries_the_corrected_text` goes red against Phase 0's document. **Phase 0's block is authoritative** — it matches the live `DatasetBundle`, which really does carry seven fields.

Verify that the block reads exactly as Phase 0 v2 Task 18 left it, and correct it toward that form if a merge disturbed it:
```python
# model/data/prepare.py
@dataclass(frozen=True)
class SplitConfig:
    seed: int = 42
    test_size: float = 0.15
    n_folds: int = 5

@dataclass(frozen=True, eq=False)
class DatasetBundle:
    train_df: "pd.DataFrame"          # deduped, contains comment_text + 6 label columns
    test_df: "pd.DataFrame"           # locked 15% held-out
    fold_indices: list[tuple["np.ndarray", "np.ndarray"]]  # (train_idx, val_idx) into train_df
    raw_sha256: str                   # digest of the CSV as delivered
    split_version: str                # realized train/test/fold membership + label fingerprint
    env_version: str                  # pinned libraries + dedup/normalizer parameters
    config: SplitConfig = field(default_factory=SplitConfig)
    # .data_version is a DERIVED property, sha256 over the three joined by ':', for
    # single-string display and for the once-only test-set ledger key only.

DEFAULT_SPLIT = SplitConfig()

def prepare_dataset(raw_csv: "Path", config: SplitConfig = DEFAULT_SPLIT) -> DatasetBundle: ...
```

**Edit 2 — the split and normalizer seams.** Phase 0 v2 Task 18 adds both. Verify they are present; add them if absent:
```python
# model/data/split.py
def make_splits(df: "pd.DataFrame", seed: int, test_size: float = 0.15, n_folds: int = 5) -> tuple["pd.DataFrame", "pd.DataFrame", list[tuple["np.ndarray", "np.ndarray"]]]: ...

# model/normalize.py  — two functions, deliberately different
def normalize(text: str) -> str: ...              # FROZEN corpus normalizer; dedup + gate + split_version
def normalize_for_serving(text: str) -> str: ...  # normalize() + confusable folding + MAX_INPUT_CHARS
```

**Edit 3 — the output contract.** Replace the `PredictionResponse` block with:
```python
# model/contract.py  (pydantic)
class LabelScore(BaseModel):
    prob: float
    flag: bool

class PredictionResponse(BaseModel):
    request_id: str
    model_version: str                # the OPAQUE public label, never the artifact digest (H14)
    labels: dict[str, LabelScore]     # keys == LABELS
    decision: Literal["allow", "review", "block"]
    max_prob: float
    latency_ms: int

def probs_to_dict(row) -> dict[str, float]: ...   # the single authoritative array->dict adapter
```

**Edit 4 — the database writes.** Replace the `backend/db.py` block with:
```python
# backend/db.py
def init_db(engine) -> None: ...                                        # idempotent create of the three tables
def write_pending(session, pending: "PendingWrite", stamp) -> int: ...   # returns latency_ms
def insert_prediction(session, row: "PredictionRow") -> None: ...        # idempotent on request_id
def enqueue_review(session, intent: "ReviewIntent") -> None: ...         # idempotent on request_id; carries sample_rate
def fetch_pending_reviews(session, limit: int) -> list["ReviewRow"]: ...
# review_queue.sample_rate is NOT NULL in (0,1] for source in ('flagged','random-audit')
# and NULL for source = 'user-report' (premortem H8, H9). See Phase 2 Task 10a.
# Phase 3 owns the reviewer and re-scorer writes:
# submit_review(session, request_id, reviewer_labels, reviewer_id)
# write_distilbert_probs(session, request_id, probs)
```

**Edit 5 — one prose correction.** In the Phase 4 files line, change `ci.yml  # lint + tests + scans + tf plan gate on PR` to `ci.yml  # lint + tests + scans + tf fmt/validate on PR (never plan — premortem H36)`.

> **Correction, 2026-07-31.** This edit also said: change "about 60 rows" to "**36 rows**". Phase 0 v2 Task 18 already changed the same sentence to "**68 rows**", and Phase 0's `test_documented_fixture_size_matches_the_committed_fixture` reads the real `tests/fixtures/mini_jigsaw.csv` and compares. 68 is the measured number; 36 was carried over from the pre-v2 fixture. **Do not touch the row count here.** The `SUPERSEDED` tuple still contains `"about 60 rows"`, so a regression to the pre-v2 text still fails this suite.

**Edit 6 — merge Phase 0's conformance suite into this one.** Phase 0 v2 Task 18 wrote `tests/unit/test_interface_contract_doc.py`, which asserts the same block with an AST parser rather than a substring parser. Move its four cases that this file does not already cover — `test_dataset_bundle_fields_match_the_documented_block`, `test_documented_bundle_no_longer_carries_the_old_data_version_field`, `test_prediction_response_decision_is_documented_as_a_literal`, `test_documented_fixture_size_matches_the_committed_fixture` — into `tests/unit/test_interface_contracts.py`, then `git rm tests/unit/test_interface_contract_doc.py`. `test_there_is_exactly_one_interface_contract_conformance_suite` above is what makes this permanent.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_interface_contracts.py -v`
Expected: 12 PASS (four original, four merged from Phase 0's suite by Edit 6, and the four added for suite uniqueness, adapter uniqueness, and the two canonical adapter messages).

If `test_every_contract_symbol_exists_with_the_declared_parameters` still reports a mismatch, the correct move is **not** to relax the test. Either the contract row is stale (fix the row) or a phase implemented a different signature than the seam it was given (fix the code). Record which, in the commit message, because a drift discovered here is a drift two phases already built against.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/plans/2026-07-01-toxic-moderation-master-plan.md tests/unit/test_interface_contracts.py
git commit -m "Make the cross-phase interface contract executable and reconcile five drifts"
```

---

### Task 12: Branch protection, applied by API and captured as evidence (premortem H10, C9)

Rubric 4.2's last sentence is the clause with the least code and the most weight: **"PRs cannot merge if checks fail."** It is a GitHub setting. Nothing in the repository configures it, nothing proves it, and the solo developer is the repository admin, who merges straight through a red gate unless "Do not allow bypassing the above settings" is explicitly ticked. Because it leaves no artifact, it **cannot be graded from the repository alone** — so this task both applies it and commits the machine-readable proof.

**Files:**
- Create: `scripts/apply_branch_protection.sh`, `scripts/verify_branch_protection.sh`, `docs/evidence/branch-protection.json`
- Modify: `Makefile`
- Test: `tests/unit/test_branch_protection.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_branch_protection.py`:
```python
"""Rubric 4.2: "PRs cannot merge if checks fail" (premortem H10).

This is repository configuration, not code. The captured configuration is the only artifact a
grader — or a future maintainer — can check from the repository, so it is committed and
asserted here. `make verify-branch-protection` re-fetches the live setting and fails on drift.
"""

import json
from pathlib import Path

EVIDENCE = Path("docs/evidence/branch-protection.json")
REQUIRED_CONTEXT = "ci-gate"


def protection() -> dict:
    assert EVIDENCE.exists(), (
        "docs/evidence/branch-protection.json is missing. Run "
        "`make branch-protection`; without it rubric 4.2's last clause has no evidence at all"
    )
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_the_required_status_check_is_the_aggregate_gate():
    contexts = protection()["required_status_checks"]["contexts"]
    assert contexts == [REQUIRED_CONTEXT], (
        f"the protected context must be exactly ['{REQUIRED_CONTEXT}']; requiring individual "
        f"job names means a renamed job silently stops being required. Got {contexts}"
    )


def test_administrators_cannot_bypass():
    """This is the GitHub setting literally labelled "Do not allow bypassing the above
    settings". Without it the solo developer, who is the admin, merges through a red gate and
    rubric 4.2 is unmet in the only way that matters."""
    assert protection()["enforce_admins"] is True, (
        "enforce_admins is false: the admin can still merge a failing pull request"
    )


def test_no_review_requirement_deadlocks_the_solo_developer():
    """enforce_admins plus a review requirement is unshippable for one developer: GitHub does
    not allow approving your own pull request, so nothing would ever merge. Recording the
    decision here stops it being 'fixed' into a deadlock later."""
    assert protection()["required_pull_request_reviews"] is False, (
        "a review requirement is configured; with enforce_admins on, a solo developer can "
        "never merge anything"
    )


def test_force_pushes_and_deletions_are_denied():
    data = protection()
    assert data["allow_force_pushes"] is False, "main can be rewritten past the gate"
    assert data["allow_deletions"] is False, "main can be deleted, taking the rule with it"


def test_the_evidence_names_this_repository_and_branch():
    data = protection()
    assert data["repository"].endswith("/mlops-toxic-moderation"), data["repository"]
    assert data["branch"] == "main"
    assert data["captured_at"], "no capture timestamp; stale evidence is indistinguishable"


def test_the_apply_script_does_not_hardcode_a_token():
    script = Path("scripts/apply_branch_protection.sh").read_text(encoding="utf-8")
    for marker in ("ghp_", "github_pat_", "GH_TOKEN="):
        assert marker not in script, f"a credential literal in a public repository: {marker}"
    assert "gh auth status" in script, "the script must fail fast on an unauthenticated shell"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_branch_protection.py -v`
Expected: FAIL — every test with `AssertionError: docs/evidence/branch-protection.json is missing. Run make branch-protection; ...` and `test_the_apply_script_does_not_hardcode_a_token` with `FileNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

`scripts/apply_branch_protection.sh` (`chmod +x`):
```bash
#!/usr/bin/env bash
# Rubric 4.2: "PRs cannot merge if checks fail."
#
# Three settings, and the interaction between them is the part that bites:
#   required_status_checks  -> the merge button is disabled while `ci-gate` is red
#   enforce_admins: true    -> the console checkbox "Do not allow bypassing the above
#                              settings". The solo developer IS the admin, so without this
#                              the rule is advisory (premortem H10)
#   required_pull_request_reviews: null
#                           -> deliberately ABSENT. GitHub does not allow approving your own
#                              pull request, so a review requirement combined with
#                              enforce_admins means a solo developer can never merge anything.
#
# Classic branch protection is used rather than a ruleset on purpose: rulesets are served by a
# different API, so `branches/main/protection` would 404 and the evidence capture below — the
# only thing a grader can check from the repository — would not exist.
set -euo pipefail

REPO="${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
BRANCH="${BRANCH:-main}"
CONTEXT="${CONTEXT:-ci-gate}"
EVIDENCE="${EVIDENCE:-docs/evidence/branch-protection.json}"

command -v gh >/dev/null || { echo "the gh CLI is required" >&2; exit 1; }
gh auth status >/dev/null
if [ "$(gh api "repos/${REPO}" --jq '.permissions.admin')" != "true" ]; then
  echo "admin rights on ${REPO} are required to set branch protection" >&2
  exit 1
fi

echo "applying branch protection to ${REPO}@${BRANCH}, required context '${CONTEXT}'"
gh api -X PUT "repos/${REPO}/branches/${BRANCH}/protection" \
  -H "Accept: application/vnd.github+json" \
  --input - >/dev/null <<JSON
{
  "required_status_checks": { "strict": false, "contexts": ["${CONTEXT}"] },
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "block_creations": false,
  "required_conversation_resolution": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON

mkdir -p "$(dirname "${EVIDENCE}")"
gh api "repos/${REPO}/branches/${BRANCH}/protection" \
  -H "Accept: application/vnd.github+json" \
  --jq '{
    required_status_checks: {
      strict: .required_status_checks.strict,
      contexts: .required_status_checks.contexts
    },
    enforce_admins: .enforce_admins.enabled,
    required_pull_request_reviews: (has("required_pull_request_reviews")),
    required_linear_history: .required_linear_history.enabled,
    allow_force_pushes: .allow_force_pushes.enabled,
    allow_deletions: .allow_deletions.enabled
  }' \
  | REPO="${REPO}" BRANCH="${BRANCH}" python3 -c '
import datetime, json, os, sys

data = json.load(sys.stdin)
data["repository"] = os.environ["REPO"]
data["branch"] = os.environ["BRANCH"]
data["captured_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
print(json.dumps(data, indent=2, sort_keys=True))
' > "${EVIDENCE}"

echo "wrote ${EVIDENCE}"
cat "${EVIDENCE}"
```

`scripts/verify_branch_protection.sh` (`chmod +x`):
```bash
#!/usr/bin/env bash
# Re-fetch the live setting and fail on any drift from the committed evidence. Run this in the
# Phase 4 gate and again before submission: a setting can be changed by one click, and the
# committed JSON would then be a confident-looking lie.
set -euo pipefail

EVIDENCE="${EVIDENCE:-docs/evidence/branch-protection.json}"
[ -f "${EVIDENCE}" ] || { echo "${EVIDENCE} is missing; run make branch-protection" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
EVIDENCE="${TMP}/live.json" ./scripts/apply_branch_protection.sh >/dev/null

python3 - "${EVIDENCE}" "${TMP}/live.json" <<'PY'
import json
import sys

def load(path):
    data = json.load(open(path, encoding="utf-8"))
    data.pop("captured_at", None)
    return data

committed, live = load(sys.argv[1]), load(sys.argv[2])
if committed != live:
    print("branch protection has drifted from the committed evidence", file=sys.stderr)
    print(f"committed: {json.dumps(committed, sort_keys=True)}", file=sys.stderr)
    print(f"live:      {json.dumps(live, sort_keys=True)}", file=sys.stderr)
    raise SystemExit(1)
print("branch protection matches the committed evidence")
PY
```

Append to the `Makefile`:
```makefile
.PHONY: branch-protection verify-branch-protection
branch-protection:
	./scripts/apply_branch_protection.sh
verify-branch-protection:
	./scripts/verify_branch_protection.sh
```

Apply it:
```bash
chmod +x scripts/apply_branch_protection.sh scripts/verify_branch_protection.sh
make branch-protection
```

**Console fallback**, if `gh` is unavailable or the API call is refused. Use classic branch protection, not a ruleset, so the evidence command above still works:

1. GitHub → the repository → **Settings** → **Branches** (left nav, under "Code and automation").
2. **Add branch protection rule**. Branch name pattern: `main`.
3. Tick **Require status checks to pass before merging**. In the search box type `ci-gate` and select it. *If it does not appear, the check has never run — open the Task 13 pull request first, let CI run once, then return here.* Leave **Require branches to be up to date before merging** unticked.
4. Leave **Require a pull request before merging** unticked. See the header comment above: with admin enforcement on, a review requirement makes a solo project unmergeable.
5. Tick **Require linear history**. Leave **Allow force pushes** and **Allow deletions** unticked.
6. Scroll to the bottom and tick **Do not allow bypassing the above settings**. This is `enforce_admins`, and it is the clause the rubric grades.
7. **Create**, then capture the evidence with the same command the script uses:
   `EVIDENCE=docs/evidence/branch-protection.json ./scripts/apply_branch_protection.sh` — it is idempotent and re-asserts the same JSON.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_branch_protection.py -v && make verify-branch-protection`
Expected: 6 PASS, then `branch protection matches the committed evidence`.

Then confirm the rule is live rather than merely recorded:
```bash
gh api repos/$(gh repo view --json nameWithOwner --jq .nameWithOwner)/branches/main/protection \
  --jq '{admins_enforced: .enforce_admins.enabled, required: .required_status_checks.contexts}'
```
Expected: `{"admins_enforced":true,"required":["ci-gate"]}`.

- [ ] **Step 5: Commit**

```bash
git add scripts/apply_branch_protection.sh scripts/verify_branch_protection.sh \
        docs/evidence/branch-protection.json Makefile tests/unit/test_branch_protection.py
git commit -m "Protect main with a required status check that admins cannot bypass"
```

---

### Task 13: Prove the gate — a blocked merge, screenshotted, then green (premortem H10)

A configuration that has never been tested against a red build is a configuration nobody has tested. Delivery spec §9 says Phase 4's approach is "prove the gate by opening a PR with a failing test and observing the block", and the observation has to leave an artifact, because the block itself does not. This task deliberately breaks the build on this branch, records GitHub refusing the merge in three independent forms, then fixes it and records the merge succeeding.

**Files:**
- Create: `tests/unit/test_gate_proof.py` (temporary, removed in this same task), `docs/evidence/blocked-merge-api.txt`, `docs/evidence/blocked-merge-cli.txt`, `docs/evidence/blocked-merge.png`, `docs/evidence/ci-gate.md`
- Test: `tests/unit/test_ci_gate_evidence.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/test_ci_gate_evidence.py`:
```python
"""Evidence that the gate was observed to block a merge (rubric 4.2, premortem H10).

Rubric 4.2's last clause cannot be graded from the repository, because a branch-protection rule
leaves no file behind. These four artifacts are the substitute: the API refusal, the CLI
refusal, a screenshot of the disabled merge button, and a short written account naming the
pull request so the whole thing can be re-checked by anyone with the repository open.
"""

from pathlib import Path

EVIDENCE = Path("docs/evidence")
SCREENSHOT = EVIDENCE / "blocked-merge.png"
API_REFUSAL = EVIDENCE / "blocked-merge-api.txt"
CLI_REFUSAL = EVIDENCE / "blocked-merge-cli.txt"
NARRATIVE = EVIDENCE / "ci-gate.md"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_the_blocked_merge_api_refusal_is_recorded():
    assert API_REFUSAL.exists(), f"{API_REFUSAL} is missing"
    text = API_REFUSAL.read_text(encoding="utf-8")
    assert "405" in text, "the merge API did not refuse; the gate was not enforcing"
    assert "ci-gate" in text, "the refusal does not name the required check"


def test_the_blocked_merge_cli_refusal_is_recorded():
    assert CLI_REFUSAL.exists(), f"{CLI_REFUSAL} is missing"
    text = CLI_REFUSAL.read_text(encoding="utf-8").lower()
    assert any(word in text for word in ("not mergeable", "blocked", "required", "failing")), (
        "the CLI output does not show a refusal"
    )


def test_the_blocked_merge_screenshot_is_a_real_png():
    assert SCREENSHOT.exists(), f"{SCREENSHOT} is missing"
    data = SCREENSHOT.read_bytes()
    assert data[:8] == PNG_MAGIC, "not a PNG"
    assert len(data) > 20_000, (
        f"{len(data)} bytes is too small to be a legible screenshot of a merge box"
    )


def test_the_narrative_names_the_pull_request_and_both_states():
    assert NARRATIVE.exists(), f"{NARRATIVE} is missing"
    text = NARRATIVE.read_text(encoding="utf-8")
    assert "pull/" in text, "the account does not link the pull request it describes"
    assert "blocked" in text.lower() and "merged" in text.lower(), (
        "the account must record BOTH states: refused while red, merged once green"
    )


def test_no_proof_test_was_left_behind():
    """The deliberate failure is a scaffold. Leaving it in place, even skipped, means the
    suite carries a test whose purpose is to fail."""
    assert not Path("tests/unit/test_gate_proof.py").exists(), (
        "remove the deliberate-failure test; the evidence files are the durable artifact"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_ci_gate_evidence.py -v`
Expected: FAIL — four tests with `AssertionError: docs/evidence/blocked-merge-api.txt is missing` and the corresponding messages; `test_no_proof_test_was_left_behind` PASSES for now.

- [ ] **Step 3: Break the build on purpose, observe the refusal, capture it**

**3a. Push the branch with a deliberate failure.**
```bash
mkdir -p docs/evidence
cat > tests/unit/test_gate_proof.py <<'PY'
def test_the_ci_gate_blocks_a_failing_pull_request():
    """Deliberate failure. This exists to prove that a red check disables the merge button,
    which is rubric 4.2's last clause. It is deleted in the same pull request once the
    refusal has been captured under docs/evidence/."""
    assert False, "deliberate failure proving the CI gate blocks merges"
PY

git add tests/unit/test_gate_proof.py tests/unit/test_ci_gate_evidence.py
git commit -m "Add a deliberately failing test to prove the CI gate blocks merges"
git push -u origin feat/phase-4-ci-gate

gh pr create --base main --title "Phase 4: test consolidation and the CI/CD gate" \
  --body "Hashed dependency locks with --require-hashes, SHA-pinned actions with per-job permissions, digest-pinned base images, ruff + pytest (unit and integration) above an 80% coverage floor, gitleaks, semgrep, pip-audit, and terraform fmt/validate without credentials. Branch protection on main requires the aggregate ci-gate check with admin bypass disabled. This commit deliberately fails, to capture evidence that the gate blocks the merge."
```

**3b. Wait for the gate to go red.**
```bash
gh pr checks --watch
```
Expected: `test` fails, therefore `ci-gate` fails. Note the PR number as `PR`.

**3c. Capture the CLI refusal.**
```bash
PR=$(gh pr view --json number --jq .number)
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)

{
  echo "\$ gh pr merge ${PR} --squash"
  gh pr merge "${PR}" --squash 2>&1 || true
} > docs/evidence/blocked-merge-cli.txt
cat docs/evidence/blocked-merge-cli.txt
```
Expected: text containing `Pull request ... is not mergeable` or `required status check`.

**3d. Capture the API refusal.** This is the strongest artifact, because it is GitHub's own status code and message rather than a client's rendering of it:
```bash
{
  echo "\$ gh api -X PUT repos/${REPO}/pulls/${PR}/merge -f merge_method=squash"
  gh api -X PUT "repos/${REPO}/pulls/${PR}/merge" -f merge_method=squash 2>&1 || true
} > docs/evidence/blocked-merge-api.txt
cat docs/evidence/blocked-merge-api.txt
```
Expected: `gh: Required status check "ci-gate" is expected. (HTTP 405)`. If the message does not name `ci-gate`, the required context does not match the job name — return to Task 12 and fix the context before continuing, because that mismatch would deadlock every future merge.

**3e. Screenshot the disabled merge box.**
```bash
gh pr view --web
```
In the browser, scroll to the merge box at the bottom of the Conversation tab. It shows the red `ci-gate` check, "Required statuses must pass before merging", and a disabled **Merge pull request** button. Capture that region — not the whole desktop — and save it as `docs/evidence/blocked-merge.png`.

Three things to check before saving, because this file is world-readable on a public repository: no browser tab or bookmark bar showing an unrelated URL, no AWS account ID or email address in frame, and no raw user text (delivery spec §6.4). Crop to the merge box and the check list.

**3f. Write the account.**

`docs/evidence/ci-gate.md`:
```markdown
# Rubric 4.2 evidence: PRs cannot merge if checks fail

`main` is protected by a classic branch-protection rule requiring the aggregate status check
`ci-gate`, with **Do not allow bypassing the above settings** enabled. The developer on this
project is the sole repository admin, so without that setting the rule would be advisory. The
live configuration is captured in `branch-protection.json` and re-verified by
`make verify-branch-protection`.

The rule was tested against a real red build rather than assumed.

| Step | State | Artifact |
|---|---|---|
| A commit with a deliberately failing test was pushed to the pull request | `ci-gate` red | `blocked-merge.png` |
| `gh pr merge --squash` | **blocked** | `blocked-merge-cli.txt` |
| `PUT /repos/.../pulls/<n>/merge` | **blocked**, HTTP 405, "Required status check \"ci-gate\" is expected." | `blocked-merge-api.txt` |
| The failing test was removed and the same pull request re-run | `ci-gate` green | it **merged** |

Pull request: https://github.com/rocklambros/mlops-toxic-moderation/pull/<n>

`ci-gate` is one job that depends on `lint`, `test`, `secrets-scan`, `sast`, `deps-audit`, and
`terraform`, and fails if any of them reports anything other than success. One protected
context rather than six means adding or renaming a job cannot silently remove it from the gate.
```

Replace `<n>` with the real pull request number in both places.

**3g. Fix, confirm green, and prove the unblocking.**
```bash
git rm tests/unit/test_gate_proof.py
git add docs/evidence
git commit -m "Record evidence that the CI gate blocked a failing pull request"
git push
gh pr checks --watch
```
Expected: every job green, `ci-gate` green, and the merge box now offers an enabled **Merge pull request** button. Do **not** merge yet — Task 14 is the gate for that.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_ci_gate_evidence.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Commit**

Already committed in step 3g. Confirm the tree is clean and the evidence is on the branch:
```bash
git status --short
git show --stat HEAD
```
Expected: no untracked evidence files; the commit lists `docs/evidence/blocked-merge-api.txt`, `blocked-merge-cli.txt`, `blocked-merge.png`, `ci-gate.md`, and the deletion of `tests/unit/test_gate_proof.py`.

---

### Task 14: Phase 4 gate, rubric self-check, and merge

- [ ] **Step 1: The full suite, lint, and coverage, exactly as CI runs them**

```bash
make lint
make db-up
make test-cov
```
Expected: ruff clean; unit and integration suites green; `Required test coverage of 80% reached`.

- [ ] **Step 2: The scanners, locally, with the same commands and versions**

```bash
make scan
```
Expected: gitleaks `no leaks found`; semgrep reports no blocking findings; `pip-audit` reports `No known vulnerabilities found` for every lock.

- [ ] **Step 3: The supply-chain assertions, as one pass**

```bash
PYTHONHASHSEED=0 .venv/bin/pytest \
  tests/unit/test_dependency_locks.py \
  tests/unit/test_install_commands.py \
  tests/unit/test_workflow_hygiene.py \
  tests/unit/test_image_pinning.py \
  tests/unit/test_vuln_ledger.py \
  tests/unit/test_interface_contracts.py \
  tests/unit/test_branch_protection.py \
  tests/unit/test_ci_gate_evidence.py \
  tests/unit/test_test_harness.py \
  tests/unit/test_coverage_policy.py \
  tests/unit/test_pin_actions.py -v
```
Expected: all PASS. This is the auditable list: every premortem finding this phase owns has at least one test in it.

- [ ] **Step 4: Prove the enforcement is still live, not merely recorded**

```bash
make verify-branch-protection
```
Expected: `branch protection matches the committed evidence`. This runs after the evidence was captured on purpose — a setting changed by one click between Task 12 and now would otherwise ship as a confident-looking lie.

- [ ] **Step 5: Rubric self-check, clause by clause**

Confirm each row by running the command in it, not by reading the plan.

| Rubric clause | Evidence | Command |
|---|---|---|
| 4.1 unit tests for individual functions | Unit suite green, auto-marked | `PYTHONHASHSEED=0 .venv/bin/pytest -m unit -q` |
| 4.1 integration tests for FastAPI endpoints using pytest | Integration suite green against a real Postgres | `make test-integration` |
| 4.2 `.github/workflows/ci.yml` exists | File present, parsed by the hygiene suite | `test -f .github/workflows/ci.yml && echo ok` |
| 4.2 triggers on pull requests to `main` | Trigger asserted | `PYTHONHASHSEED=0 .venv/bin/pytest -k test_ci_triggers_on_pull_requests_to_main -q` |
| 4.2 runs a linter | `ruff check .` in the `lint` job | `gh run view --log --job lint \| grep -c "ruff"` |
| 4.2 runs the full test suite | Both halves run in the `test` job | `PYTHONHASHSEED=0 .venv/bin/pytest -k test_ci_runs_a_linter_and_the_full_test_suite -q` |
| **4.2 PRs cannot merge if checks fail** | HTTP 405 refusal, CLI refusal, screenshot, live re-verification | `cat docs/evidence/blocked-merge-api.txt && make verify-branch-protection` |

- [ ] **Step 6: Merge the pull request through the gate it just built**

```bash
gh pr checks
gh pr merge --squash --delete-branch
git checkout main && git pull
```
Expected: every check green, the merge succeeds, and `main` now carries the workflow that guards it. That merge is itself the final piece of evidence — the same rule that refused the red commit accepted the green one.

Then confirm `main` is genuinely protected against the thing the master plan forbids:
```bash
git commit --allow-empty -m "probe: direct push to main must be refused" && git push
```
Expected: `remote: error: GH006: Protected branch update failed` naming the required status check. Undo the local probe commit:
```bash
git reset --hard origin/main
```

## Self-Review

**Spec coverage.**

| Source clause | Where it lands |
|---|---|
| Rubric 4.1 unit tests | Task 4 (markers, auto-application), Task 14 step 5 |
| Rubric 4.1 integration tests for FastAPI endpoints with pytest | Task 4 (fake-green guard), Task 8 (`test` job with a real Postgres service), Task 14 |
| Rubric 4.2 `ci.yml` on pull requests to `main` | Tasks 7, 8 |
| Rubric 4.2 a linter and the full test suite | Tasks 7, 8; `test_ci_runs_a_linter_and_the_full_test_suite` requires both halves, not one |
| **Rubric 4.2 PRs cannot merge if checks fail** | Tasks 12, 13 — configuration, machine-readable evidence, empirical proof, and live re-verification |
| Delivery spec §6.3 hashed lock (`--require-hashes`) | Tasks 1, 2, 3 — including the lock generator itself, which was the remaining hole |
| Delivery spec §6.3 no secret in a build arg or image layer | Task 7 `test_no_workflow_passes_a_secret_as_a_docker_build_arg` |
| Delivery spec §9 Phase 4 approach: "prove the gate" | Task 13 |
| Delivery spec §8 the CI gate is never cut | The gate is one workflow file plus one branch rule; Task 12's evidence is what makes its absence detectable |
| Master plan Phase 4 tasks 1–5 | Task 8 (ci.yml), Task 9 (gitleaks/semgrep/pip-audit), Tasks 4–5 (markers, coverage), Task 11 (cross-cutting gap: the executable interface contract), Task 7 (`test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded`) |
| Premortem H10, H35, H36, C11, H1 (CI half), H24, C9 (4.2 rows) | Coverage map in the front matter; every row names an owning task and a test that fails if unfixed |

**Placeholder scan.** Every step carries real code and an exact command. No TODO, no "handle edge cases", no "similar to". Four values are deliberately resolved by a command rather than transcribed, each with the command inline, because writing a fabricated hash into a supply-chain control would be worse than having none: the action commit SHAs (Task 8, `python -m scripts.pin_actions`), the `postgres:16-alpine` digest (Tasks 8 and 10, `docker buildx imagetools inspect`), the Terraform version (Task 8, from the installed binary), and the gitleaks release checksums (Task 8, from the pinned release's own checksums file). Three tasks have no new implementation code by design — Task 7 ships assertions that Task 8 satisfies, Task 13 ships a procedure whose output is the artifact, and Task 14 is a gate — and each says so explicitly rather than inventing filler.

**Type consistency.** `pin_text(text, resolve)` takes and returns `str` and is the only writer of `uses:` lines; `resolve_with_gh` matches the `Resolver` alias it is passed as. `parse_ledger` returns `list[Suppression]`, consumed by `active_ids` and `expired` with the same element type and by `scripts/run_pip_audit.sh` as one id per stdout line. The required status check context is the string `ci-gate` in exactly three places — the `ci-gate` job id and name in `ci.yml`, `CONTEXT` in `apply_branch_protection.sh`, and `REQUIRED_CONTEXT` in two test modules — and `test_the_gate_job_name_matches_the_protected_context` plus `test_the_required_status_check_is_the_aggregate_gate` fail if they diverge, which is the failure that would otherwise deadlock every merge on a context that never reports. `TEST_DATABASE_URL` carries the same `postgresql+psycopg://` DSN shape in the Makefile, the CI `test` job, and Phase 3's integration conftest. The coverage floor is the literal `--cov-fail-under=80` in the Makefile and in `ci.yml`, asserted by string in both.

**Known residual, stated rather than hidden.** Three things this phase does not close.

First, semgrep's `p/python` and `p/secrets` rule packs are fetched from the semgrep registry at run time and are not content-pinned, so the SAST job's behaviour can change without a commit. Vendoring the rules would pin it and would also freeze SAST coverage at day 12; the rules are analysis inputs rather than executed code, so the trade is accepted and named.

Second, `test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded` accepts a recorded cut in `docs/cut-log.md` as satisfying the assertion. That is deliberate — the day-8 checkpoint can legitimately remove the reaper along with DistilBERT — but it means the strongest cost control in the project can be removed by writing one line in a markdown file. The compensating control is that the line has to be written, so the removal is visible in a diff.

Third, branch protection is verified against the live API only when `make verify-branch-protection` is run by a human with admin rights. It cannot run inside `ci.yml`, because doing so would require giving pull-request CI a token that can read repository administration — precisely the write-scope widening this phase spends four tests preventing. The mitigation is that Task 14 runs it immediately before merging and the submission checklist runs it again before the deliverables are captured.

## Execution Handoff

Two options:
1. **Subagent-Driven (recommended):** fresh subagent per task, review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution:** in-session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.

**Ordering constraints.** Tasks 1–6 have no external dependency and can run in any order among themselves. Task 7 must precede Task 8, which is the whole point of writing the assertions first. Task 10 needs Phase 3's Dockerfiles to exist; if Phase 3 slipped, run it after those land and let the test stay red in the meantime rather than weakening it. Task 12 needs `gh` authenticated with admin rights on the repository. Task 13 needs Task 8 and Task 12 both complete, and it is the only task that cannot be rehearsed — it changes the state of a real pull request.

**One thing to do before Task 1.** `make lock` recompiles every dependency surface in the project. Run it early in the day, not late: if a package in `security.in` or `train.in` has no aarch64 wheel, the compile fails loudly and the fix is a version bump and a re-resolve, which is a 20-minute problem in the morning and a lost evening at 11 p.m.
