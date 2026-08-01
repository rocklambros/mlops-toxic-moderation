"""Suite-wide determinism, marker, and fake-green guards.

Three properties, each of which was a memo before it was a test.

1. **PYTHONHASHSEED=0 for every pytest invocation, not only `make test`** (premortem H1).
   The seed must be set BEFORE the interpreter starts, so no pytest ini option and no plugin
   can retroactively fix it -- and any plugin that writes os.environ["PYTHONHASHSEED"] during
   startup would defeat an env-var check while changing nothing. The interpreter's own flag
   cannot be spoofed, so that is what this reads.

2. **Markers follow the directory layout.** A marker applied by hand is a marker that gets
   forgotten on the file that matters, and `-m "not integration"` is only trustworthy if
   every test under tests/integration carries the mark whether or not anyone remembered. The
   hook is additive but not indiscriminate: a test under tests/unit that declares
   `@pytest.mark.integration` for itself keeps it and does NOT also become a unit test,
   because "unit" is a claim about needing no external service.

3. **A green run has to have proved something** (rubric 4.1). Inside CI two shapes report
   success while measuring nothing: a run that executed no test at all -- an over-narrow
   `-m` expression, a stray `--collect-only`, a path that no longer exists -- and an
   integration run in which every integration test skipped because the database it needed
   was not there. Both exit 0 today. `fake_green_reasons` is that decision, kept as a pure
   function so it can be tested against every shape rather than only the one that happened
   to occur: a guard that has never been observed refusing anything is indistinguishable
   from `return []`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Directory name -> marker. `awsapply` is declared for Phase A2's tests/infra suite, which
# needs a real AWS session and must never be selected by pull-request CI (premortem H36).
DIRECTORY_MARKERS = (
    ("integration", "integration"),
    ("perf", "perf"),
    ("infra", "awsapply"),
)
# Markers that make "unit" a lie: a test carrying one of these needs something outside the
# process, so tests/unit/test_run_cli.py's real-corpus check does not become a unit test just
# by living under tests/unit.
NOT_A_UNIT_TEST = frozenset({"integration", "perf", "awsapply"})

_RUN = {"executed": 0, "integration_selected": 0, "integration_passed": 0}


def running_in_ci(environ: dict[str, str] | None = None) -> bool:
    """GitHub Actions sets CI=true on every runner."""
    source = os.environ if environ is None else environ
    return source.get("CI", "").lower() == "true"


def fake_green_reasons(
    *, executed: int, integration_selected: int, integration_passed: int
) -> list[str]:
    """Why this run must not be reported as a pass. An empty list means it proved something."""
    reasons: list[str] = []
    if executed == 0:
        reasons.append(
            "no test executed: nothing reached the call phase and passed or failed. A run "
            "that collected nothing, deselected everything, or was invoked with "
            "--collect-only exits 0 and proves nothing"
        )
    if integration_selected and integration_passed == 0:
        reasons.append(
            f"{integration_selected} integration tests were selected and not one of them "
            "passed. An integration suite that skips itself when its database is absent is a "
            "green job that never connected to anything (rubric 4.1)"
        )
    return reasons


def pytest_configure(config: pytest.Config) -> None:
    if sys.flags.hash_randomization:
        raise pytest.UsageError(
            "PYTHONHASHSEED=0 is required: string hash randomization is ON, so any "
            "accidental dependence on set/dict iteration order is environment-dependent. "
            "Run `make test`, or in CI set `env: {PYTHONHASHSEED: '0'}` on the job."
        )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """tryfirst, because `-m` deselection happens in this same hook: a marker added after the
    mark plugin has run would never affect selection, and `-m unit` would select nothing."""
    root = config.rootpath
    for item in items:
        path = Path(str(item.path))
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:  # a test file outside the repository
            parts = set(path.parts)
        for directory, marker in DIRECTORY_MARKERS:
            if directory in parts:
                item.add_marker(getattr(pytest.mark, marker))
        if "unit" in parts and not ({m.name for m in item.iter_markers()} & NOT_A_UNIT_TEST):
            item.add_marker(pytest.mark.unit)


def pytest_collection_finish(session: pytest.Session) -> None:
    selected = [item for item in session.items if item.get_closest_marker("integration")]
    _RUN["integration_selected"] = len(selected)
    if selected and running_in_ci() and not os.environ.get("TEST_DATABASE_URL"):
        raise pytest.UsageError(
            "TEST_DATABASE_URL is unset inside CI and this run selected "
            f"{len(selected)} integration tests. CI wires a `services: postgres` container "
            "and passes its URL; without it the suite silently falls back to starting a "
            "container of its own, which is a different thing being tested on a runner that "
            "may not permit it. Set TEST_DATABASE_URL on the job that runs `-m integration`."
        )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """A skip is not an execution. That distinction is the whole guard: an integration suite
    that skips itself reports 'N skipped' and exits 0."""
    if report.when != "call" or report.skipped:
        return
    _RUN["executed"] += 1
    if report.passed and "integration" in getattr(report, "keywords", {}):
        _RUN["integration_passed"] += 1


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not running_in_ci():
        return
    reasons = fake_green_reasons(
        executed=_RUN["executed"],
        integration_selected=_RUN["integration_selected"],
        integration_passed=_RUN["integration_passed"],
    )
    if not reasons:
        return
    for reason in reasons:
        print(f"FAKE-GREEN GUARD: {reason}", file=sys.stderr)
    if int(exitstatus) in (0, int(pytest.ExitCode.NO_TESTS_COLLECTED)):
        session.exitstatus = 1
