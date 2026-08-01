"""Repo-wide GitHub Actions hygiene, written before there is a workflow to check.

Premortem H35: unpinned third-party Actions (any of which can mint the `gha-deploy` OIDC
token), no per-job `permissions:` block against a repository default of *write*, unpinned base
images defeating SHA traceability.
Premortem H36: `terraform plan` on pull requests is code execution on attacker-supplied `.tf`.
Premortem H1: CI runs bare `pytest`, so the Makefile's PYTHONHASHSEED pin does not apply.
Rubric 4.2: ci.yml on pull requests to main, running a linter and the full test suite.

**Why this file is shaped the way it is.** A hygiene suite written against a workflow that
does not exist yet has one obvious failure mode and one subtle one. The obvious one is passing
vacuously over an empty directory, which `scanned_workflows()` prevents by failing loudly
instead of certifying an empty set. The subtle one is worse: every assertion here could be
*wrong* -- a regex that never matches, a key read from the wrong level -- and a red
`FileNotFoundError` would look identical to a correct test waiting for its input. So every
check is a pure function over parsed YAML, exercised right now against a known-good sample
workflow and against one deliberately broken variant per check. The checks are therefore
proven today; only their input is missing.

`GOOD_CI` below doubles as the specification for Task 8. A `.github/workflows/ci.yml` shaped
like it satisfies every assertion in this file.

Note on YAML: `on:` is parsed by PyYAML as the boolean True, because YAML 1.1 treats `on` as a
truthy scalar. Every reader here goes through `triggers()` for that reason.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(".github/workflows")
CI = WORKFLOWS / "ci.yml"
REAPER = WORKFLOWS / "runpod-reaper.yml"
CUT_LOG = Path("docs/cut-log.md")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<ref>[^\s#]+)\s*(?P<trailer>#.*)?$")
TAG_COMMENT_RE = re.compile(r"#\s*v?\d+[\w.\-/]*")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}")
PINNED_RUNNERS = {"ubuntu-24.04-arm", "ubuntu-24.04"}
REQUIRED_CONTEXT = "ci-gate"
NO_WORKFLOWS = (
    ".github/workflows holds no workflow file, so this scan would certify an empty set. "
    "Task 8 of the Phase 4 plan writes .github/workflows/ci.yml; this assertion is red until "
    "it does, on purpose -- a green scan over nothing is exactly what this file exists to "
    "prevent."
)


# --------------------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------------------


def workflow_files() -> list[Path]:
    if not WORKFLOWS.exists():
        return []
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def scanned_workflows() -> list[Path]:
    """Every repo-wide scan goes through here, so none of them can pass on an empty set."""
    files = workflow_files()
    if not files:
        pytest.fail(NO_WORKFLOWS)
    return files


def parse(text: str) -> dict:
    return yaml.safe_load(text) or {}


def load(path: Path) -> dict:
    return parse(path.read_text(encoding="utf-8"))


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


def step_run(step: dict) -> str:
    """`run: true` parses as a YAML boolean, and a scan that raises TypeError on it is a scan
    that has stopped scanning. Every reader of `run:` goes through here."""
    return str(step.get("run") or "")


def run_text(doc: dict) -> str:
    return "\n".join(step_run(step) for job in jobs(doc).values() for step in steps(job))


def job_env(doc: dict, job: dict) -> dict:
    merged = dict(doc.get("env") or {})
    merged.update(job.get("env") or {})
    return merged


def step_env(doc: dict, job: dict, step: dict) -> dict:
    merged = job_env(doc, job)
    merged.update(step.get("env") or {})
    return merged


# --------------------------------------------------------------------------------------
# checks: each is a pure function returning the offenders it found
# --------------------------------------------------------------------------------------


def unpinned_actions(name: str, text: str) -> list[str]:
    """H35. A movable tag in ANY job is in the OIDC blast radius."""
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = USES_RE.match(line)
        if match is None:
            continue
        ref = match.group("ref").strip("'\"")
        if ref.startswith((".", "docker://")):
            continue
        if "@" not in ref:
            offenders.append(f"{name}:{lineno} {ref} (no ref at all)")
            continue
        if not SHA_RE.match(ref.rsplit("@", 1)[1]):
            offenders.append(f"{name}:{lineno} {ref}")
    return offenders


def actions_missing_a_version_comment(name: str, text: str) -> list[str]:
    """A bare 40-hex string is unmaintainable: nobody can tell v4.2.2 from v3 six weeks
    later, so nobody upgrades it, so the pin rots into an unpatched dependency."""
    offenders = []
    for lineno, line in enumerate(text.splitlines(), 1):
        match = USES_RE.match(line)
        if match is None or match.group("ref").startswith((".", "docker://")):
            continue
        if not TAG_COMMENT_RE.search(match.group("trailer") or ""):
            offenders.append(f"{name}:{lineno} {line.strip()}")
    return offenders


def workflows_without_top_level_permissions(name: str, doc: dict) -> list[str]:
    return [] if "permissions" in doc else [name]


def jobs_without_permissions(name: str, doc: dict) -> list[str]:
    return [f"{name}:{job_id}" for job_id, job in jobs(doc).items() if "permissions" not in job]


def jobs_with_a_write_scope(name: str, doc: dict) -> list[str]:
    """Pull-request CI holds no write scope at all, `id-token` least of all (H35, H36)."""
    offenders = []
    for job_id, job in jobs(doc).items():
        perms = job.get("permissions")
        if perms in ("write-all", "read-all") or perms is True:
            offenders.append(f"{name}:{job_id} -> {perms}")
            continue
        if isinstance(perms, dict):
            offenders += [
                f"{name}:{job_id} -> {scope}: write"
                for scope, level in perms.items()
                if level == "write"
            ]
    return offenders


def undigested_images(name: str, doc: dict) -> list[str]:
    offenders = []
    for job_id, job in jobs(doc).items():
        candidates = []
        container = job.get("container")
        if isinstance(container, str):
            candidates.append(container)
        elif isinstance(container, dict) and "image" in container:
            candidates.append(container["image"])
        for service in (job.get("services") or {}).values():
            if isinstance(service, dict) and "image" in service:
                candidates.append(service["image"])
        offenders += [
            f"{name}:{job_id} -> {image}"
            for image in candidates
            if not DIGEST_RE.search(str(image))
        ]
    return offenders


def unpinned_runners(name: str, doc: dict) -> list[str]:
    """`ubuntu-latest` silently changes the OS, the preinstalled toolchain, and the CPU
    architecture the hashed locks were resolved for. This project's locks are compiled on
    aarch64 and its images are built for arm64 Graviton, so the runner label is load-bearing
    rather than cosmetic."""
    return [
        f"{name}:{job_id} -> {job.get('runs-on')}"
        for job_id, job in jobs(doc).items()
        if isinstance(job.get("runs-on"), str) and job["runs-on"] not in PINNED_RUNNERS
    ]


def secrets_passed_as_build_args(name: str, text: str) -> list[str]:
    """Delivery spec 6.3: a build-arg or ENV bakes a credential into an image layer
    permanently. BuildKit secret mounts are the only sanctioned path."""
    return [
        f"{name}:{lineno} {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), 1)
        if re.search(r"(build-args?|--build-arg)", line) and "secrets." in line
    ]


def continue_on_error_sites(name: str, doc: dict) -> list[str]:
    offenders = []
    for job_id, job in jobs(doc).items():
        if job.get("continue-on-error"):
            offenders.append(f"{name}:{job_id} (job)")
        offenders += [
            f"{name}:{job_id} step {index}"
            for index, step in enumerate(steps(job))
            if step.get("continue-on-error")
        ]
    return offenders


def pull_request_target_workflows(name: str, doc: dict) -> list[str]:
    return [name] if "pull_request_target" in triggers(doc) else []


def aggregate_gate_problems(doc: dict) -> list[str]:
    """D1. Branch protection pins check *contexts* by name, so requiring six job names means
    every future job added or renamed silently stops being required, and a renamed job leaves
    a required context that never reports, deadlocking every merge."""
    problems = []
    all_jobs = jobs(doc)
    gate = all_jobs.get(REQUIRED_CONTEXT)
    if gate is None:
        return [
            f"there is no `{REQUIRED_CONTEXT}` job; branch protection pins one context and "
            "without the aggregate job every future job silently stops being required"
        ]
    needs = gate.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    missing = sorted(set(all_jobs) - {REQUIRED_CONTEXT} - set(needs))
    if missing:
        problems.append(f"{REQUIRED_CONTEXT} does not depend on: {missing}")
    if str(gate.get("if")).strip() != "always()":
        problems.append(
            "the gate is not `if: always()`; a skipped upstream job would skip the gate, and "
            "a skipped required check reports as pending forever"
        )
    body = "\n".join(step_run(step) for step in steps(gate))
    if "needs" not in str(gate) or "exit 1" not in body:
        problems.append(
            "the gate must inspect every upstream result and exit non-zero on anything but "
            "success"
        )
    if gate.get("name", REQUIRED_CONTEXT) != REQUIRED_CONTEXT:
        problems.append(
            "the required status check is matched by the check-run name, which is the job's "
            "`name` when set; a mismatch deadlocks every merge on a context that never reports"
        )
    return problems


def ci_contract_problems(doc: dict, text: str) -> list[str]:
    """Everything rubric 4.2 and the premortem require of `ci.yml` specifically. Task 8's
    contract, in one list."""
    problems = []
    on = triggers(doc)
    if "pull_request" not in on:
        problems.append("rubric 4.2: ci.yml does not trigger on pull requests")
    else:
        branches = (on.get("pull_request") or {}).get("branches") or []
        if "main" not in branches:
            problems.append(f"rubric 4.2: pull requests to `main`, not {branches}")

    runs = run_text(doc)
    if "ruff check" not in runs:
        problems.append("rubric 4.2 names a linter; no `ruff check` runs")
    if not re.search(r"pytest[^\n]*-m\s+[\"']not integration[\"']", runs):
        problems.append("the unit half of the suite is not run")
    if not re.search(r"pytest[^\n]*-m\s+integration\b", runs):
        problems.append(
            "rubric 4.1 requires integration tests for the FastAPI endpoints; 'full test "
            "suite' in 4.2 means both halves"
        )
    if "--cov-fail-under=80" not in text:
        problems.append("the coverage floor is not enforced in CI")
    for markers, why in (
        (("gitleaks",), "secret scanning gate"),
        (("semgrep",), "SAST gate"),
        (("pip-audit", "run_pip_audit.sh"), "dependency vulnerability scan"),
    ):
        if not any(marker in text for marker in markers):
            problems.append(f"no {markers[0]}: {why}")

    if "terraform fmt -check" not in runs:
        problems.append("terraform formatting drift is not checked")
    if "terraform init -backend=false" not in runs:
        problems.append("`init` without -backend=false reaches for remote state, and therefore"
                        " credentials")
    if "terraform validate" not in runs:
        problems.append("terraform is never validated")
    if "terraform plan" in text:
        problems.append(
            "`plan` executes provider binaries, `data \"external\"` programs, and module "
            "source fetches against .tf files the pull-request author controls, and the "
            "rubric does not ask for it (premortem H36)"
        )
    for marker in ("configure-aws-credentials", "role-to-assume", "AWS_ACCESS_KEY_ID"):
        if marker in text:
            problems.append(f"ci.yml reaches for AWS credentials via {marker}")

    for job_id, job in jobs(doc).items():
        perms = job.get("permissions")
        if isinstance(perms, dict) and perms.get("id-token") == "write":
            problems.append(
                f"ci.yml:{job_id} can mint an OIDC token; that is the deploy path, not the "
                "pull-request path (premortem H4, H36)"
            )
        for step in steps(job):
            command = step_run(step)
            if re.search(r"\|\|\s*true", command) or command.strip().startswith("set +e"):
                problems.append(f"ci.yml:{job_id} swallows the exit status of a check")
            if "pytest" not in command:
                continue
            environment = step_env(doc, job, step)
            if str(environment.get("PYTHONHASHSEED")) != "0":
                problems.append(
                    f"ci.yml:{job_id} runs pytest without PYTHONHASHSEED=0 (premortem H1)"
                )
            if re.search(r"-m\s+integration\b", command) and not environment.get(
                "TEST_DATABASE_URL"
            ):
                problems.append(
                    f"ci.yml:{job_id} runs `-m integration` without TEST_DATABASE_URL; the "
                    "conftest fake-green guard refuses that run, and without the guard the "
                    "suite would silently start a container of its own"
                )
    return problems


# --------------------------------------------------------------------------------------
# the checks, exercised now against a known-good workflow and one broken variant each
# --------------------------------------------------------------------------------------

# The specification for Task 8. Every SHA and digest below is a deliberately obvious
# PLACEHOLDER -- Task 8 resolves the real ones with `python -m scripts.pin_actions` and
# `docker buildx imagetools inspect postgres:16-alpine`. They are not fabricated claims about
# real commits; they are all-zero strings that could not be mistaken for one.
PLACEHOLDER_SHA = "0" * 40
PLACEHOLDER_DIGEST = "0" * 64
GOOD_CI = f"""
name: ci
on:
  pull_request:
    branches: [main]
permissions: {{}}
env:
  PYTHONHASHSEED: "0"
jobs:
  lint:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2
      - run: ruff check .
  test:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    services:
      postgres:
        image: postgres:16-alpine@sha256:{PLACEHOLDER_DIGEST}
    env:
      TEST_DATABASE_URL: postgresql+psycopg://postgres:postgres@localhost:5432/postgres
    steps:
      - uses: actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2
      - run: pytest -m "not integration" --cov --cov-report=
      - run: pytest -m integration --cov --cov-append --cov-fail-under=80
  secrets-scan:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2
      - run: ./bin/gitleaks detect --no-banner --redact
  sast:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2
      - run: semgrep --error --config p/python
  deps-audit:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2
      - run: scripts/run_pip_audit.sh
  terraform:
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2
      - run: terraform fmt -check -recursive
      - run: terraform init -backend=false
      - run: terraform validate
  ci-gate:
    needs: [lint, test, secrets-scan, sast, deps-audit, terraform]
    if: always()
    runs-on: ubuntu-24.04-arm
    permissions:
      contents: read
    steps:
      - name: Every upstream job must have succeeded
        run: |
          for result in ${{{{ join(needs.*.result, ' ') }}}}; do
            [ "$result" = "success" ] || {{ echo "upstream result: $result"; exit 1; }}
          done
"""


def good() -> tuple[dict, str]:
    return parse(GOOD_CI), GOOD_CI


def test_the_sample_workflow_is_the_specification_and_it_parses():
    doc, _ = good()
    assert set(jobs(doc)) == {
        "lint", "test", "secrets-scan", "sast", "deps-audit", "terraform", "ci-gate"
    }


def test_every_check_passes_on_the_known_good_workflow():
    """The direction that keeps the rest of this file from being a set of checks that refuse
    everything. If any check is red here, it is the check that is wrong, not ci.yml."""
    doc, text = good()
    found = (
        unpinned_actions("good", text)
        + actions_missing_a_version_comment("good", text)
        + workflows_without_top_level_permissions("good", doc)
        + jobs_without_permissions("good", doc)
        + jobs_with_a_write_scope("good", doc)
        + undigested_images("good", doc)
        + unpinned_runners("good", doc)
        + secrets_passed_as_build_args("good", text)
        + continue_on_error_sites("good", doc)
        + pull_request_target_workflows("good", doc)
        + aggregate_gate_problems(doc)
        + ci_contract_problems(doc, text)
    )
    assert found == [], found


@pytest.mark.parametrize(
    ("old", "new", "check", "expected"),
    [
        (f"actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2", "actions/checkout@v4.2.2",
         "unpinned_actions", "actions/checkout@v4.2.2"),
        (f"actions/checkout@{PLACEHOLDER_SHA}  # v4.2.2", f"actions/checkout@{PLACEHOLDER_SHA}",
         "actions_missing_a_version_comment", "uses:"),
        ("permissions: {}\n", "", "workflows_without_top_level_permissions", "good"),
        ("  lint:\n    runs-on: ubuntu-24.04-arm\n    permissions:\n      contents: read\n",
         "  lint:\n    runs-on: ubuntu-24.04-arm\n", "jobs_without_permissions", "good:lint"),
        ("    permissions:\n      contents: read\n    services:",
         "    permissions:\n      id-token: write\n    services:",
         "jobs_with_a_write_scope", "id-token: write"),
        (f"postgres:16-alpine@sha256:{PLACEHOLDER_DIGEST}", "postgres:16-alpine",
         "undigested_images", "postgres:16-alpine"),
        ("  lint:\n    runs-on: ubuntu-24.04-arm", "  lint:\n    runs-on: ubuntu-latest",
         "unpinned_runners", "ubuntu-latest"),
        ("      - run: ruff check .",
         "      - run: docker build --build-arg TOKEN=${{ secrets.GH_TOKEN }} .",
         "secrets_passed_as_build_args", "build-arg"),
        ("      - run: ruff check .", "      - continue-on-error: true\n        run: ruff check .",
         "continue_on_error_sites", "good:lint"),
        ("  pull_request:\n    branches: [main]",
         "  pull_request_target:\n    branches: [main]",
         "pull_request_target_workflows", "good"),
    ],
)
def test_each_check_fires_on_the_shape_it_exists_to_catch(old, new, check, expected):
    """One broken variant per check. Without this the whole file could be regexes that never
    match, and a missing ci.yml would make it look correct."""
    doc, text = good()
    broken = text.replace(old, new, 1)
    assert broken != text, f"the sample no longer contains {old!r}; this mutation is inert"
    checker = globals()[check]
    offenders = checker("good", parse(broken) if "doc" in checker.__code__.co_varnames else broken)
    assert any(expected in offender for offender in offenders), (
        f"{check} did not fire on its own failure shape; found {offenders}"
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("    branches: [main]", "    branches: [develop]", "not ['develop']"),
        ("      - run: ruff check .\n", "", "names a linter"),
        ('pytest -m "not integration"', "pytest -k nothing", "unit half"),
        ("pytest -m integration --cov --cov-append --cov-fail-under=80",
         "pytest -k nothing", "both halves"),
        ("--cov-fail-under=80", "--cov-report=term", "coverage floor"),
        ("./bin/gitleaks detect --no-banner --redact", "echo disabled", "gitleaks"),
        ("semgrep --error --config p/python", "echo disabled", "semgrep"),
        ("scripts/run_pip_audit.sh", "echo disabled", "pip-audit"),
        ("terraform init -backend=false", "terraform init", "-backend=false"),
        ("      - run: terraform validate\n", "      - run: terraform plan\n", "H36"),
        ("      - run: terraform validate\n",
         "      - uses: aws-actions/configure-aws-credentials@v4\n",
         "configure-aws-credentials"),
        ('  PYTHONHASHSEED: "0"', "  UNRELATED: x", "PYTHONHASHSEED=0"),
        ("      TEST_DATABASE_URL: postgresql+psycopg://postgres:postgres@localhost:5432/postgres",
         "      UNRELATED: x", "TEST_DATABASE_URL"),
        ("      - run: ruff check .", "      - run: ruff check . || true", "swallows the exit"),
    ],
)
def test_the_ci_contract_fires_on_every_clause_it_owns(old, new, expected):
    _, text = good()
    broken = text.replace(old, new, 1)
    assert broken != text, f"the sample no longer contains {old!r}; this mutation is inert"
    problems = " | ".join(ci_contract_problems(parse(broken), broken))
    assert expected in problems, f"the contract missed {expected!r}; it reported: {problems}"


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("  ci-gate:\n", "  ci-gateway:\n", "there is no `ci-gate` job"),
        ("needs: [lint, test, secrets-scan, sast, deps-audit, terraform]",
         "needs: [lint]", "does not depend on"),
        ("    if: always()\n", "", "if: always()"),
        ('[ "$result" = "success" ] || { echo "upstream result: $result"; exit 1; }',
         'echo "$result"', "exit non-zero"),
        ("  ci-gate:\n    needs:", "  ci-gate:\n    name: gate\n    needs:", "check-run name"),
    ],
)
def test_the_aggregate_gate_check_fires_on_every_way_the_gate_can_be_defeated(old, new, expected):
    _, text = good()
    broken = text.replace(old, new, 1)
    assert broken != text, f"the sample no longer contains {old!r}; this mutation is inert"
    problems = " | ".join(aggregate_gate_problems(parse(broken)))
    assert expected in problems, f"the gate check missed {expected!r}; it reported: {problems}"


# --------------------------------------------------------------------------------------
# the same checks, applied to the workflows this repository actually ships
# --------------------------------------------------------------------------------------


def test_the_ci_workflow_exists():
    assert CI.exists(), (
        "rubric 4.2 names .github/workflows/ci.yml explicitly, and Task 8 of the Phase 4 plan "
        "is what writes it. Every assertion above has already been proved against GOOD_CI in "
        "this file, so a ci.yml shaped like that sample turns this and the scans below green."
    )


def test_every_workflow_parses():
    for path in scanned_workflows():
        assert isinstance(load(path), dict), f"{path} is not a YAML mapping"


def test_every_third_party_action_is_pinned_to_a_full_commit_sha():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in unpinned_actions(path.name, path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "SHA-pin these actions; a movable tag in ANY job is in the OIDC blast radius "
        "(premortem H35). Run `python -m scripts.pin_actions`:\n  " + "\n  ".join(offenders)
    )


def test_every_pinned_action_keeps_its_tag_as_a_comment():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in actions_missing_a_version_comment(
            path.name, path.read_text(encoding="utf-8")
        )
    ]
    assert not offenders, f"pinned actions with no human-readable version comment: {offenders}"


def test_every_workflow_declares_a_top_level_permissions_block():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in workflows_without_top_level_permissions(path.name, load(path))
    ]
    assert not offenders, (
        "the repository default GITHUB_TOKEN permission is write; a workflow without a "
        "top-level permissions block inherits it (premortem H35): " + ", ".join(offenders)
    )


def test_every_job_declares_its_own_permissions_block():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in jobs_without_permissions(path.name, load(path))
    ]
    assert not offenders, f"jobs with no explicit permissions: {offenders}"


def test_no_job_in_pull_request_ci_grants_a_write_scope():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in jobs_with_a_write_scope(path.name, load(path))
        if path.name == CI.name
    ]
    assert not offenders, (
        "pull-request CI must hold no write scope at all, including id-token "
        "(premortem H35, H36): " + ", ".join(offenders)
    )


def test_container_images_in_workflows_are_pinned_by_digest():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in undigested_images(path.name, load(path))
    ]
    assert not offenders, f"pin these images by digest (premortem H35): {offenders}"


def test_runners_are_pinned_labels_not_latest():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in unpinned_runners(path.name, load(path))
    ]
    assert not offenders, (
        "`ubuntu-latest` silently changes the OS, the preinstalled toolchain, and the CPU "
        "architecture the hashed locks were resolved for: " + ", ".join(offenders)
    )


def test_no_workflow_passes_a_secret_as_a_docker_build_arg():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in secrets_passed_as_build_args(path.name, path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"secret passed as a build argument: {offenders}"


def test_no_step_is_marked_continue_on_error():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in continue_on_error_sites(path.name, load(path))
    ]
    assert not offenders, f"a check that cannot fail is not a check: {offenders}"


def test_no_workflow_uses_pull_request_target():
    offenders = [
        offender
        for path in scanned_workflows()
        for offender in pull_request_target_workflows(path.name, load(path))
    ]
    assert not offenders, (
        "pull_request_target runs attacker-authored code with the base repository's secrets "
        "and a write token (premortem H36): " + ", ".join(offenders)
    )


def test_the_ci_workflow_meets_the_rubric_and_premortem_contract():
    if not CI.exists():
        pytest.fail(NO_WORKFLOWS)
    text = CI.read_text(encoding="utf-8")
    problems = ci_contract_problems(parse(text), text)
    assert not problems, "ci.yml does not meet its contract:\n  " + "\n  ".join(problems)


def test_the_aggregate_gate_job_exists_and_covers_every_other_job():
    if not CI.exists():
        pytest.fail(NO_WORKFLOWS)
    problems = aggregate_gate_problems(load(CI))
    assert not problems, "\n  ".join(problems)


# --------------------------------------------------------------------------------------
# the scheduled reaper, if it survived the cut line
# --------------------------------------------------------------------------------------


def test_the_runpod_reaper_is_scheduled_or_its_cut_is_recorded():
    """A GPU pod left running is the one cost failure with no ceiling. Phase 1 Task 18 owns
    `.github/workflows/runpod-reaper.yml`; `docs/cut-log.md` is the escape hatch, and
    tests/unit/test_cut_log.py exists so that escape hatch cannot open on a FileNotFoundError.
    """
    if not REAPER.exists():
        assert CUT_LOG.exists() and "runpod-reaper" in CUT_LOG.read_text(encoding="utf-8"), (
            "the reaper is absent from this branch and no cut is recorded. Restore "
            "`.github/workflows/runpod-reaper.yml` (Phase 1 Task 18), or record the cut in "
            "docs/cut-log.md naming `runpod-reaper` and the item from the ordered cut list. "
            "This is not Task 8's to fix: it is a decision, and leaving it undecided is how a "
            "sweep pod bills for a week"
        )
        return
    on = triggers(load(REAPER))
    assert "schedule" in on, "the reaper only works if something runs it"
    assert "workflow_dispatch" in on, (
        "a reaper you cannot trigger by hand is a reaper you cannot use during an incident"
    )
