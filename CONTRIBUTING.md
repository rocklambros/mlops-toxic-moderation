# Contributing

Thanks for looking. This is a course project with a single maintainer, so the process below
is short by design. It is written down because a convention nobody can find is a convention
nobody follows.

## Development setup

Python 3.11, Docker with Compose v2, and `make`. Nothing in this section touches AWS.

```bash
git clone https://github.com/rocklambros/mlops-toxic-moderation.git
cd mlops-toxic-moderation
make venv          # 3.11 virtualenv, installed from the hashed lock with --require-hashes
make lint test     # ruff, then the unit suite
```

`make venv` refuses a Python that is not 3.11. That is deliberate: the locks are compiled for
one interpreter and a 3.12 wheel set would resolve differently.

## Running the tests

```bash
make lint                 # ruff check .
make test                 # the unit suite, PYTHONHASHSEED=0
make test-integration     # needs a running stack, see below
make test-cov             # unit suite with the coverage floor enforced
```

`PYTHONHASHSEED=0` is not a preference. `conftest.py` refuses to run the suite under hash
randomisation, because several tests assert on iteration order that is stable only when the
seed is fixed. If you invoke `pytest` directly, set it:

```bash
PYTHONHASHSEED=0 .venv/bin/pytest tests/unit/test_policy.py -q
```

The integration suite needs the local stack up:

```bash
export DEMO_API_KEY="$(openssl rand -hex 16)"
export REVIEWER_SHARED_SECRET="$(openssl rand -hex 16)"
export SUBMITTER_FP_KEY="$(openssl rand -hex 16)"
docker compose -f infra/docker-compose.yml up -d --build
make test-integration
```

## Where the tests live

| Directory | What it covers | Needs |
|---|---|---|
| `tests/unit/` | Pure functions, contracts, and every document under test | Nothing |
| `tests/integration/` | FastAPI endpoints, database round trips, the deployed traversal | A running stack |
| `tests/infra/` | Shell scripts and Terraform, exercised against stubs | Nothing |
| `tests/perf/` | The latency budget | A running stack |

Tests in this repository routinely assert on prose. `tests/unit/test_readme.py`,
`test_model_card.py`, `test_security_md.py` and `test_rubric_matrix.py` all read Markdown and
fail when a document stops being true. If you change a document and a test goes red, read the
test before editing it: the assertion usually encodes a decision, and the docstring says
which one.

## Dependencies

Every install verifies hashes. There is no exception, and `tests/unit/test_install_commands.py`
scans the repository to keep it that way.

To add or change a dependency, edit the relevant `requirements/*.in` and recompile:

```bash
make lock          # recompiles the locks with pip-compile --generate-hashes
```

Never hand-edit a `requirements/*.txt`. They are generated, and a hand-added line without a
hash makes the hashed install refuse the whole file rather than just that line.

That phrasing is deliberate, by the way: this file avoids writing the literal install command
because `tests/unit/test_install_commands.py` walks the whole tree and fails when one appears
somewhere nothing checks it for `--require-hashes`. Root-level prose is outside its scan
roots, so the guard would have to be widened to accommodate the sentence. Rewording the
sentence is cheaper than weakening the control.

## Branches and commits

Branch from `main`. Name it for the change, not the ticket: `fix/awslogs-blocking-stall`,
`docs/rubric-conformance`.

Commit messages explain **why**, not what. The diff already says what changed. A message that
survives a year later says what was wrong and what the alternative was. Present tense,
imperative subject, no trailing period on the subject line.

## Pull requests

Every pull request runs seven checks: `lint`, `test`, `secrets-scan`, `sast`, `deps-audit`,
`terraform`, and an aggregate `ci-gate`. All seven must be green. Branch protection refuses a
merge otherwise, and that refusal has been exercised rather than assumed. See
[`docs/evidence/ci-gate.md`](docs/evidence/ci-gate.md).

Do not merge with `--admin`. The point of the gate is that it cannot be bypassed, and a
bypass in the history undoes the demonstration.

A useful pull request body states what was wrong, what the fix is, and what evidence says the
fix works. If a test changed, say why the old assertion no longer holds.

## Code style

`ruff` is the only linter and it is the arbiter. Line length is 100. Run `make lint` before
pushing.

Two conventions ruff does not enforce, both load-bearing here:

- **Comments say why.** A comment restating the code is noise. A comment explaining the
  constraint that produced the code is the reason the file is maintainable.
- **Fail closed.** Where a check cannot be completed, refuse rather than continue. The model
  loader, the digest verification, and the deploy gate all follow this, and a change that
  turns a refusal into a warning will be sent back.

## Security

Do not open a public issue for a vulnerability. [`SECURITY.md`](SECURITY.md) has the private
reporting path, the scope, and what to expect.

Nothing secret goes in the repository. `scripts/redact.py` is the single place that knows
what has to disappear from a published artifact, and `make submission-check` runs it over the
documents that get published.

## Review

One maintainer, so review is a single pass, usually within a day or two. Expect questions
about why rather than about style: style is ruff's job.

## Releases

The maintainer owns deployment. A merge to `main` that touches code triggers
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), which builds images, rolls
the containers through SSM, and gates on a live health check across all three instances.
Infrastructure changes are never unattended: `terraform apply` is an operator action, and the
CI deploy role is denied the permissions that would let a workflow replace an instance.
